"""Explicit per-review financial candidate, exact Decimal mathematics only."""
from decimal import Decimal,ROUND_HALF_UP
from fractions import Fraction
from . import controls
from .quotation_workbooks import number,InvalidWorkbook

CONTRACT='USD_PACK_4DP_LINE_HALF_UP_CENTS_V1'
DESCRIPTION='USD; pack prices ≤4 decimals; each line half-up to cents, then sum; explicit freight + tax − discount; no inferred zero; legacy unit representation half-up to 4 decimals, with exact delta retained'


def amount(value,scale,label):
 try:return number(value,scale=scale,limit='999999999999.99' if scale==2 else '9999999999.9999',label=label)
 except InvalidWorkbook as e:raise controls.InvalidPolicy(str(e)) from None


def calculate(snapshot,response,choices,components,*,contract,unit_adapter_approved,release_remainder):
 blocking=[];results=[];total=Decimal(0)
 if contract!=CONTRACT:blocking.append('Explicit per-review approval of the displayed USD financial candidate is required')
 if unit_adapter_approved is not True:blocking.append('Approve the displayed four-decimal legacy unit representation and retained rounding differences')
 if release_remainder is not True:blocking.append('Explicitly approve final resolution and release of unfilled request quantities')
 if not isinstance(choices,dict) or set(choices)!={l['row_id'] for l in snapshot['lines']}:raise controls.InvalidPolicy('Explicit quantity/price decisions for every request line are required')
 reports={l['line_id']:l for l in response['lines']}
 for line in snapshot['lines']:
  key=line['row_id'];choice=choices[key];reported=reports[key]
  if not isinstance(choice,dict) or set(choice)!={'packs','price_action','pack_price','reason','quantity_reason','excess_reason'}:raise controls.InvalidPolicy('Exact owner line allocation, price action and explanations required')
  packs=choice['packs'];reason=choice['reason'];qreason=choice['quantity_reason'];excess=choice['excess_reason']
  if not isinstance(packs,list) or len(packs)!=4 or any(type(p) is not int or not 0<=p<=1000000 for p in packs):raise controls.InvalidPolicy('Enter four nonnegative whole-pack allocations explicitly')
  if any(not isinstance(v,str) or len(v)>2000 for v in (reason,qreason,excess)):raise controls.InvalidPolicy('Invalid owner explanation')
  count=sum(packs);units=[p*line['pack_size'] for p in packs]
  if sum(units)>2147483647:raise controls.InvalidPolicy('Approved units exceed the existing accounting capacity')
  if count and (reported['available_packs'] is None or count>reported['available_packs']) and not qreason.strip():blocking.append(line['name']+': explicit quantity exception reason required for unconfirmed availability')
  if count>line['total_packs'] and not excess.strip():blocking.append(line['name']+': explicit revised excess-quantity authorization reason required')
  quoted=reported['quoted_pack_price'];action=choice['price_action'];price=None
  if action not in ('ACCEPT_QUOTE','REJECT','UNRESOLVED','OWNER_EXCEPTION'):raise controls.InvalidPolicy('Choose an explicit price-review action')
  if count:
   if action=='ACCEPT_QUOTE':
    if quoted is None:blocking.append(line['name']+': vendor price is missing; request a revision or choose an explicit owner exception')
    else:price=quoted
   elif action=='OWNER_EXCEPTION':
    if not reason.strip():blocking.append(line['name']+': explicit alternative-valuation reason required')
    price=amount(choice['pack_price'],4,line['name']+' approved pack valuation')
    if price is None:blocking.append(line['name']+': explicit owner pack valuation required')
   else:blocking.append(line['name']+': rejected or unresolved price cannot create a commitment')
  if price is not None:
   value=Decimal(price);raw=value*count;rounded=raw.quantize(Decimal('.01'),rounding=ROUND_HALF_UP);unit=(value/line['pack_size']).quantize(Decimal('.0001'),rounding=ROUND_HALF_UP)
   if unit>Decimal('9999999999.9999'):raise controls.InvalidPolicy('Unit valuation exceeds legacy accounting precision')
   total+=rounded;delta=rounded-unit*sum(units)
  else:raw=rounded=unit=delta=None
  expected=line['expected_unit_cost'];comparison='UNKNOWN' if expected is None or quoted is None else 'EQUAL' if Fraction(Decimal(quoted))/line['pack_size']==Fraction(Decimal(expected)) else 'DISCREPANCY'
  results.append(dict(line,kind='PO',requested_packs=line['packs'],requested_total_packs=line['total_packs'],requested_pack_size=line['pack_size'],packs=packs,units=units,total_packs=count,total_units=sum(units),reported=reported,price_comparison=comparison,quote_decision=action,owner_reason=reason.strip(),quantity_reason=qreason.strip(),excess_reason=excess.strip(),accepted_pack_price=price,line_raw=str(raw) if raw is not None else None,line_total=str(rounded) if rounded is not None else None,unit_adapter_delta=str(delta) if delta is not None else None,valuation=dict(source='VENDOR_QUOTE' if action=='ACCEPT_QUOTE' else 'OWNER_EXPLICIT',unit_value=str(unit),reason=reason.strip() or 'Owner explicitly accepts this vendor pack quotation under the reviewed financial contract',supplier_quote=quoted,actual_invoice=None) if unit is not None else None))
 approved={};component_decisions={}
 if not isinstance(components,dict) or set(components)!={'freight','tax','discount'}:raise controls.InvalidPolicy('Explicit freight, tax and discount decisions required; blanks are unknown')
 for key,choice in components.items():
  if not isinstance(choice,dict) or set(choice)!={'source','amount','reason'} or choice['source'] not in ('VENDOR_QUOTE','OWNER_EXCEPTION') or not isinstance(choice['reason'],str) or len(choice['reason'])>2000:raise controls.InvalidPolicy('Invalid component approval')
  if choice['source']=='VENDOR_QUOTE':value=response['components'][key]
  else:
   if not choice['reason'].strip():blocking.append(key+': explicit owner exception reason required')
   value=amount(choice['amount'],2,key+' approved amount')
  if value is None:blocking.append(key+': quoted amount is unknown; approve an explicit treatment or request a revised quotation')
  approved[key]=value;component_decisions[key]=dict(choice,approved_amount=value,vendor_amount=response['components'][key])
 complete=all(v is not None for v in approved.values()) and all(l['total_packs']==0 or l['line_total'] is not None for l in results)
 net=total+Decimal(approved['freight'])+Decimal(approved['tax'])-Decimal(approved['discount']) if complete else None
 if net is not None and not Decimal(0)<=net<=Decimal('999999999999.99'):blocking.append('Approved document total must fit the existing nonnegative accounting capacity')
 if not any(l['total_packs']>0 for l in results):blocking.append('No positive commitment quantity. Cancel the pending request explicitly if nothing is being purchased')
 financial=dict(contract=contract,description=DESCRIPTION,currency='USD',merchandise=str(total),**approved,total=str(net.quantize(Decimal('.01'))) if net is not None else None,complete=complete,component_decisions=component_decisions,unit_adapter_approved=unit_adapter_approved,release_remainder=release_remainder)
 return results,financial,blocking
