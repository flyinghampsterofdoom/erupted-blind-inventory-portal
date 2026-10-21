"""Scoped management explanations, separate from internal solver diagnostics."""
from collections import Counter
from datetime import timedelta
from fractions import Fraction


def explain_optimization(*, positions, original, assignments, names, targets,
                         preferred_days, effective_dates, avoid_stores, store_names):
    changes = [r for r in positions if original[r['id']] != assignments[r['id']]]
    if not changes:
        return []
    notes = []
    before = Counter((original[r['id']], r['week']) for r in positions)
    after = Counter((assignments[r['id']], r['week']) for r in positions)
    for week in sorted({r['week'] for r in changes}):
        people = sorted(e for e, w in targets if w == week)
        loss = lambda counts: sum(Fraction(counts[e, week] - targets[e, week],
                                          max(1, targets[e, week])) ** 2 for e in people)
        if loss(after) >= loss(before):
            continue
        changed = [e for e in people if before[e, week] != after[e, week]]
        rows = [r for r in changes if r['week'] == week]
        totals = '; '.join(f'{names[e]} {before[e, week]}/{targets[e, week]} → '
                           f'{after[e, week]}/{targets[e, week]}' for e in changed)
        notes.append(dict(code='ALLOCATION_BALANCED', employee_ids=changed,
            store_ids=sorted({r['store_id'] for r in rows}),
            message=f'Adjusted {len(rows)} unlocked assignment'+('s' if len(rows) != 1 else '')+
                    f' to balance weekly allocations: {totals}.'))
    affected = {e for r in changes for e in (original[r['id']], assignments[r['id']]) if e is not None}
    days_in_plan = {r['date'] for r in positions}
    # A repaired row may have belonged to someone no longer in the roster.
    for eid in sorted(affected.intersection(preferred_days)):
        days = sorted(set(effective_dates.get(eid, ())))
        blocks = []
        for day in days:
            if not blocks or blocks[-1][-1] + timedelta(days=1) != day:
                blocks.append([])
            blocks[-1].append(day)
        for block in blocks:
            preferred = preferred_days[eid]
            if len(block) <= preferred or not days_in_plan.intersection(block):
                continue
            stores = {r['store_id'] for r in positions
                      if assignments[r['id']] == eid and r['date'] in block}
            notes.append(dict(code='CONSECUTIVE_PREFERENCE_OVERRIDDEN', employee_ids=[eid],
                store_ids=sorted(stores), message=f'{names[eid]} prefers at most {preferred} '
                f'consecutive workdays; the plan uses {len(block)} ({block[0]}–{block[-1]}). '
                'This is a soft preference; the organizational three-day maximum and required rest remain enforced.'))
    for row in changes:
        eid = assignments[row['id']]
        if (eid, row['store_id']) in avoid_stores:
            notes.append(dict(code='STORE_PREFERENCE_OVERRIDDEN', employee_ids=[eid],
                store_ids=[row['store_id']], message=f'{names[eid]} is assigned to '
                f'{store_names[row["store_id"]]} on {row["date"]}, marked Avoid if possible. '
                'AVOID is a soft preference; NEVER restrictions remain enforced.'))
    return notes
