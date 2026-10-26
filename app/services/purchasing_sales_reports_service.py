"""Funding report adapter for the shared sales-financing policy."""
from datetime import datetime,timezone,date
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select,text
from app.models import FundingReport,FundingReportLine,FundingReportFactLink,FundingAccount
from app.services import purchasing_sales_finance_service as sales
from app.services.purchasing_classification_service import account_category,ClassificationContext,LOCK
from app.services.purchasing_payments_service import account_map,digest


def snapshot(db,*,account,start_date,end_date,store_ids,query=''):
    basis=sales.calculate(db,start_date=start_date,end_date=end_date,store_ids=store_ids,include_returns=True,query=query)
    category=account_category(account,ClassificationContext(db).vendors)
    if category=='UNKNOWN':raise ValueError('This account is outside the configured Streamline / Big / Credit Card financing model')
    role=None;balances={};advice_error=None;recommended=None
    if category=='CREDIT_CARD_POOL':
        accounts,_=account_map(db)
        role=next((r for r in ('card1','card2') if accounts[r] and accounts[r].id==account.id),None)
        pool=[r for r in basis['rows'] if r['category']==category]
        balances,shares,advice_error=sales.split_pool(db,pool,accounts)
        if shares is not None:
            recommended={r:str(sum((Decimal(s[r]) for s in shares),Decimal(0))) for r in ('card1','card2')}
    # Combined pool COGS is independent of issuer balances and individual payments.
    # A report is a source/cost snapshot, not a per-card debt allocation.
    rows=[{**r,'allocated_cogs':r['cogs']} for r in basis['rows'] if r['category']==category]
    issues=[r for r in basis['issues'] if r['category'] in (category,'UNKNOWN')]
    limitations=[r for r in basis['historical_limitations'] if r['category'] in (category,'UNKNOWN')]
    known=sum((Decimal(r['allocated_cogs']) for r in rows if r['allocated_cogs'] is not None),Decimal(0))
    result={'semantics':sales.SEMANTICS,'category':category,'role':role,'all_vendors':category=='CREDIT_CARD_POOL',
        'basis':'NET_SALES_LESS_ITEMIZED_RETURNS','rows':rows,'issues':issues,
        'historical_limitations':limitations,'historical_cogs_complete':not issues and not limitations,
        'calculation_scope':'KNOWN_COST_SALES_WITH_DISCLOSED_HISTORICAL_LIMITATIONS' if limitations else 'ALL_CLASSIFIED_SALES',
        'report_mode':'COMBINED_POOL' if category=='CREDIT_CARD_POOL' else 'ACCOUNT_COGS',
        'allocation_error':None,'allocation_recommendation_unavailable':advice_error,'recommended_allocation':recommended,
        'issuer_balances':{k:str(v) if v is not None else None for k,v in balances.items()},
        'known_cogs':str(known),'calculated_cogs':None if issues else str(known),
        'detected_sale_units':str(sum((Decimal(r['quantity']) for r in rows+issues+limitations if not r['is_return'] and r['quantity'] is not None),Decimal(0))),
        'detected_return_units':str(sum((Decimal(r['quantity']) for r in rows+issues+limitations if r['is_return'] and r['quantity'] is not None),Decimal(0)))}
    result['token']=digest({k:v for k,v in result.items() if k not in ('issuer_balances','recommended_allocation','allocation_recommendation_unavailable','role')});return result


def create_report(db,*,account,start_date,end_date,store_ids,sku_filter,internal_note,overlap_acknowledged,actor_id,ip=None):
    from app.services import v2_funding_reports_service as funding
    from app.services.audit_service import log_audit
    readiness=funding.assert_funding_report_source_ready(db,start_date=start_date,end_date=end_date)
    vendor_id=account.vendor_id if account.account_type=='CONSIGNMENT' else None
    overlaps=funding.overlapping_reports(db,account_id=account.id,vendor_id=vendor_id,start_date=start_date,end_date=end_date)
    if overlaps and not overlap_acknowledged:raise ValueError('OVERLAP_ACKNOWLEDGEMENT_REQUIRED')
    data=snapshot(db,account=account,start_date=start_date,end_date=end_date,store_ids=store_ids,query=sku_filter.strip())
    report=FundingReport(account_id=account.id,vendor_id=vendor_id,
        report_number=f'COGS-SALES-{account.id}-{uuid4().hex[:16]}',account_name_snapshot=account.display_name,account_type_snapshot=account.account_type,
        sales_start_date=start_date,sales_end_date=end_date,store_ids=sorted(set(store_ids)),sku_filter=sku_filter.strip() or None,
        internal_note=internal_note.strip() or None,status='DRAFT',created_by_principal_id=actor_id,
        overlap_acknowledged=bool(overlaps),overlapping_report_ids=[r.id for r in overlaps],
        units_sold=Decimal(data['detected_sale_units']),units_returned=Decimal(data['detected_return_units']),
        net_units=Decimal(data['detected_sale_units'])-Decimal(data['detected_return_units']),
        calculated_cogs=Decimal(data['calculated_cogs']) if data['calculated_cogs'] is not None else None,
        inventory_units_snapshot=None,inventory_value_snapshot=None,
        warning_summary={'sales_classification':data,'square_source_readiness':funding._source_readiness_snapshot(readiness),
            'purchase_order_scope':{'allocation_semantics':sales.SEMANTICS,'allocation_method':sales.SEMANTICS,
                'message':'Square sold quantities × established unit cost, classified by SKU. All vendors included for credit cards. No receipt or PO eligibility gate.',
                'purchase_order_ids':[],'source_lines':[],'setup_issues':[],'eligible_sku_count':len({r['variation_id'] for r in data['rows']+data['issues']}),'fifo_exception_count':0},
            'fifo_exceptions':{'pending':0,'included':0,'ignored':0},'vendor_purchase_order_ids':[]})
    db.add(report);db.flush()
    for row in data['rows']+data['issues']+data.get('historical_limitations',[]):
        qty=Decimal(row['quantity'] or 0);returned=row['is_return'];amount=row.get('allocated_cogs')
        line=FundingReportLine(report_id=report.id,normalized_sku=row['sku'] or row['variation_id'] or 'UNKNOWN',sku_snapshot=row['sku'] or '',
            square_variation_id=row['variation_id'],product_name_snapshot=row['product'],store_id=row['store_id'],
            units_sold=0 if returned else qty,units_returned=qty if returned else 0,net_units=-qty if returned else qty,
            inventory_units_snapshot=None,inventory_value_snapshot=None,
            unit_cost_snapshot=Decimal(row['unit_cost']) if row['unit_cost'] is not None else None,
            extended_cogs=Decimal(amount) if amount is not None else None,mapping_effective_date_snapshot=start_date,
            source_transaction_count=1,warning_state='HISTORICAL_COST_UNAVAILABLE' if row.get('policy') else 'SALES_CLASSIFICATION' if 'message' not in row else 'MISSING_COST_OR_ATTRIBUTION')
        db.add(line);db.flush()
        if amount is not None:
            db.add(FundingReportFactLink(report_id=report.id,report_line_id=line.id,sale_fact_id=None if returned else row['fact_id'],
                return_fact_id=row['fact_id'] if returned else None,allocated_quantity=qty,cogs_amount_snapshot=Decimal(amount)))
    log_audit(db,actor_principal_id=actor_id,action='FUNDING_REPORT_CALCULATED',session_id=None,ip=ip,
        metadata={'report_id':report.id,'semantics':sales.SEMANTICS,'classification_token':data['token'],'all_vendors':data['all_vendors']})
    db.flush();return report


def assert_current(db,report):
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':LOCK})
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':'purchasing-exception:CARD:roles'})
    display=db.get(FundingAccount,report.account_id)
    accounts,_=account_map(db)
    ids={report.account_id}|({a.id for r,a in accounts.items() if r.startswith('card') and a} if display.account_type=='CREDIT_CARD' else set())
    db.execute(select(FundingAccount.id).where(FundingAccount.id.in_(ids)).order_by(FundingAccount.id).with_for_update()).all()
    db.expire_all()
    if report.status!='DRAFT':raise ValueError('Only a current draft can be finalized')
    data=(report.warning_summary or {}).get('sales_classification')
    if not data:raise ValueError('Preserved legacy draft: calculate a fresh sales-classification report; historical snapshots are unchanged')
    from app.models import ConsignmentSaleFact,ConsignmentReturnFact
    if data:
        for model,is_return in ((ConsignmentSaleFact,False),(ConsignmentReturnFact,True)):
            source_ids=[r['fact_id'] for r in data['rows']+data.get('historical_limitations',[]) if r['is_return']==is_return]
            db.execute(select(model.id).where(model.id.in_(source_ids)).order_by(model.id).with_for_update()).all()
    current=snapshot(db,account=db.get(FundingAccount,report.account_id),start_date=report.sales_start_date,end_date=report.sales_end_date,store_ids=report.store_ids or [],query=report.sku_filter or '')
    if current['token']!=data['token']:raise ValueError('SKU classifications, costs or sales changed. Calculate a fresh report before finalizing')
    if current['calculated_cogs'] is None:raise ValueError('Resolve the listed product cost or attribution exceptions before finalizing')
    assert_sources_unclaimed(db,report_id=report.id,account_id=report.account_id,data=current)



def vendor_snapshot(db,*,vendor,start_date,end_date):
    category=ClassificationContext.automatic({vendor.square_vendor_id}).category
    if category=='CREDIT_CARD_POOL':raise ValueError('Use the combined credit-card report for vendors outside Streamline and Big Wholesale')
    basis=sales.calculate(db,start_date=start_date,end_date=end_date,include_returns=True)
    result={'semantics':sales.SEMANTICS,'category':category,'rows':[r for r in basis['rows'] if r['category']==category],
        'issues':[r for r in basis['issues'] if r['category'] in (category,'UNKNOWN')],
        'historical_limitations':[r for r in basis['historical_limitations'] if r['category'] in (category,'UNKNOWN')]}
    result['token']=digest(result);return result



def assert_sources_unclaimed(db,*,report_id=None,account_id=None,data):
    """Only complementary card shares may reuse finalized source activity.

    A historical reclassification never creates a second payable in another
    financing category or in the older consignment statement ledger.
    """
    from sqlalchemy import or_
    from app.models import ConsignmentReport,ConsignmentReportFactLink
    sale_ids=[r['fact_id'] for r in data['rows'] if not r['is_return']]
    return_ids=[r['fact_id'] for r in data['rows'] if r['is_return']]
    conditions=or_(FundingReportFactLink.sale_fact_id.in_(sale_ids),FundingReportFactLink.return_fact_id.in_(return_ids))
    prior=db.scalars(select(FundingReport).join(FundingReportFactLink,FundingReportFactLink.report_id==FundingReport.id).where(
        FundingReport.status.not_in(('DRAFT','VOIDED')),FundingReport.id!=report_id if report_id else True,conditions)).unique().all()
    if data.get('report_mode')=='COMBINED_POOL':
        from app.services.v2_funding_reports_service import is_credit_card_pool_report
        if any(is_credit_card_pool_report(r) for r in prior):raise ValueError('Square sales already appear in a finalized combined card pool report; use the existing report or an explicit correction.')
        return
    from app.services.v2_funding_reports_service import is_credit_card_pool_report
    for existing in prior:
        if is_credit_card_pool_report(existing):continue
        previous=(existing.warning_summary or {}).get('sales_classification') or {}
        complementary=(account_id is not None and existing.account_id!=account_id and previous.get('category')==data['category']=='CREDIT_CARD_POOL'
            and previous.get('role')!=data.get('role') and previous.get('issuer_balances')==data.get('issuer_balances') and previous.get('basis')==data.get('basis'))
        if complementary:
            old={(r['is_return'],r['fact_id']):r for r in previous['rows']}
            for row in data['rows']:
                other=old.get((row['is_return'],row['fact_id']))
                if other and (other['cogs']!=row['cogs'] or Decimal(other['allocated_cogs'])+Decimal(row['allocated_cogs'])!=Decimal(row['cogs'])):
                    complementary=False;break
        if not complementary:raise ValueError(f'Square sales already appear in finalized report #{existing.id}; use an explicit financial correction rather than creating a second obligation')
    other=db.scalar(select(ConsignmentReport.id).join(ConsignmentReportFactLink,ConsignmentReportFactLink.report_id==ConsignmentReport.id).where(
        ConsignmentReport.status.in_(('FINALIZED','EMAILED')),
        or_(ConsignmentReportFactLink.sale_fact_id.in_(sale_ids),ConsignmentReportFactLink.return_fact_id.in_(return_ids))).limit(1))
    if other:raise ValueError(f'Square sales already appear in finalized consignment statement #{other}; use the existing correction workflow')
