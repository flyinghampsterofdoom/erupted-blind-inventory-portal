"""The sole actual-card balance authority. Legacy/planning ledgers are not inputs.

Every writer locks FundingAccount before allocating a sequence. Checkpoints and
activity share that lock, making their ordering independent of wall-clock time.
"""
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import func, select
from app.config import settings
from app.models import FundingAccount, FundingActualActivity, FundingBalanceCheckpoint, Principal
from app.services.purchasing_evidence_service import quantity
from app.services.audit_service import log_audit


def _money(value):
    parsed = quantity(value)
    if parsed is None or abs(parsed) >= Decimal('1000000000000') or parsed != parsed.quantize(Decimal('.01')):
        raise ValueError('Enter a finite balance with at most two decimal places.')
    return parsed.quantize(Decimal('.01'))


def _key(value):
    if not value or not str(value).strip() or len(str(value)) > 128:
        raise ValueError('A request identity is required.')
    return str(value)


def authorize_owner(db, actor_id):
    principal = db.get(Principal, actor_id)
    if (not settings.purchasing_v3_phase1_enabled or settings.purchasing_owner_principal_id != actor_id
            or principal is None or not principal.active):
        raise PermissionError('Only the configured owner can record actual card activity.')


def _account(db, account_id, actor_id):
    authorize_owner(db, actor_id)
    account = db.scalar(select(FundingAccount).where(FundingAccount.id == account_id).with_for_update())
    if account is None or account.account_type != 'CREDIT_CARD' or not account.is_active:
        raise ValueError('Choose an active credit-card account.')
    return account


def actual_card_balance(db, *, account_id):
    # One statement gives both inputs the same MVCC snapshot at READ COMMITTED.
    # Scalar columns also avoid a stale checkpoint from the ORM identity map.
    checkpoint = select(FundingBalanceCheckpoint.balance, FundingBalanceCheckpoint.covered_sequence).where(
        FundingBalanceCheckpoint.account_id == account_id
    ).order_by(FundingBalanceCheckpoint.id.desc()).limit(1).subquery()
    change = select(func.coalesce(func.sum(FundingActualActivity.signed_amount), 0)).where(
        FundingActualActivity.account_id == account_id,
        FundingActualActivity.sequence > checkpoint.c.covered_sequence
    ).correlate(checkpoint).scalar_subquery()
    value = db.scalar(select(checkpoint.c.balance + change))
    return None if value is None else _money(value)


def _sequence(db, account_id):
    return int(db.scalar(select(func.coalesce(func.max(FundingActualActivity.sequence), 0)).where(
        FundingActualActivity.account_id == account_id)) or 0)


def adjust_balance(db, *, account_id, balance, actor_id, idempotency_key):
    _account(db, account_id, actor_id)
    value, key = _money(balance), _key(idempotency_key)
    prior_request = db.scalar(select(FundingBalanceCheckpoint).where(
        FundingBalanceCheckpoint.account_id == account_id, FundingBalanceCheckpoint.idempotency_key == key))
    if prior_request:
        if prior_request.balance != value:
            raise ValueError('This request identity already recorded a different balance.')
        return prior_request
    prior = actual_card_balance(db, account_id=account_id)
    row = FundingBalanceCheckpoint(account_id=account_id, balance=value, previous_balance=prior,
        covered_sequence=_sequence(db, account_id), idempotency_key=key, created_by_principal_id=actor_id)
    db.add(row); db.flush()
    log_audit(db, actor_principal_id=actor_id, action='CARD_BALANCE_CHECKPOINT', session_id=None, ip=None,
              metadata={'account_id': account_id, 'previous': str(prior) if prior is not None else None,
                        'balance': str(value), 'covered_sequence': row.covered_sequence})
    return row


def record_actual_activity(db, *, account_id, kind, amount, actor_id, idempotency_key,
                           payment_id=None, ledger_entry_id=None, original_activity_id=None, historical=False):
    _account(db, account_id, actor_id)
    value, key = _money(amount), _key(idempotency_key)
    signs = {'CHARGE': 1, 'FEE': 1, 'INTEREST': 1, 'PAYMENT': -1, 'CREDIT': -1}
    if historical:
        raise ValueError('Historical activity may already be covered; reconcile with Adjust Balance.')
    original = None
    if kind == 'REVERSAL':
        original = db.get(FundingActualActivity, original_activity_id)
        if original is None or original.account_id != account_id or original.kind == 'REVERSAL':
            raise ValueError('Choose an original activity for this account.')
        checkpoint = db.scalar(select(FundingBalanceCheckpoint).where(FundingBalanceCheckpoint.account_id == account_id)
                               .order_by(FundingBalanceCheckpoint.id.desc()).limit(1))
        value = -original.signed_amount
    elif kind not in signs or value <= 0:
        raise ValueError('Choose valid actual activity and a positive amount.')
    else:
        value *= signs[kind]
    prior = db.scalar(select(FundingActualActivity).where(FundingActualActivity.account_id == account_id,
                                                        FundingActualActivity.idempotency_key == key))
    if prior:
        if (prior.kind, prior.signed_amount, prior.payment_id, prior.ledger_entry_id, prior.original_activity_id) != (kind, value, payment_id, ledger_entry_id, original_activity_id):
            raise ValueError('Request identity already used for different activity.')
        return prior
    if original and checkpoint is not None and original.sequence <= checkpoint.covered_sequence:
        raise ValueError('That activity is covered by a balance correction; use Adjust Balance.')
    if original and db.scalar(select(FundingActualActivity.id).where(FundingActualActivity.original_activity_id == original.id)):
        raise ValueError('Activity was already reversed.')
    row = FundingActualActivity(account_id=account_id, sequence=_sequence(db, account_id)+1,
        kind=kind, signed_amount=value, idempotency_key=key, payment_id=payment_id, ledger_entry_id=ledger_entry_id,
        original_activity_id=original_activity_id, created_by_principal_id=actor_id)
    db.add(row); db.flush()
    log_audit(db, actor_principal_id=actor_id, action='CARD_ACTUAL_ACTIVITY_RECORDED', session_id=None, ip=None,
              metadata={'account_id': account_id, 'activity_id': row.id, 'kind': kind, 'signed_amount': str(value)})
    return row


def existing_actual_request(db, *, account_id, actor_id, key):
    """Serialize retries before the compatibility ledger/payment row is created."""
    _account(db, account_id, actor_id)
    return db.scalar(select(FundingActualActivity).where(FundingActualActivity.account_id == account_id,
        FundingActualActivity.idempotency_key == _key(key)))


def uses_actual_authority(db, *, account_id):
    """Adoption is sticky: pausing new writers must never restore the mixed ledger as truth."""
    if settings.purchasing_v3_phase1_enabled:
        return True
    from sqlalchemy import inspect
    # Supports minimal legacy test databases; deployed startup requires the additive migration.
    if not inspect(db.connection()).has_table('funding_balance_checkpoints'):
        return False
    if db.scalar(select(FundingBalanceCheckpoint.id).where(
            FundingBalanceCheckpoint.account_id == account_id).limit(1)) is not None:return True
    # Known activity can precede the optional first issuer checkpoint. Pausing
    # writers must not turn an unknown issuer balance into a legacy ledger sum.
    if not inspect(db.connection()).has_table('funding_actual_activity'):return False
    return db.scalar(select(FundingActualActivity.id).where(
        FundingActualActivity.account_id == account_id).limit(1)) is not None
