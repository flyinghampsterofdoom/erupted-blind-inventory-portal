"""Private transactional Flavor Intelligence administration on the existing store.

Routes supply authenticated existing principal IDs and retain generated previews
server-side. Clients submit only preview IDs, never a trusted plan or actor ID.
No Square, authentication provider or filesystem calls occur here.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib,json,uuid
from sqlalchemy import insert,select,update,func
from app.services.flavor_explorer.storage import _snapshot as _stored_snapshot,_json,_now,SNAPSHOT_FIELDS
AXES={"sweetness","tartness","creaminess","richness","brightness","cooling","crispness"}
def _identifier(value):
    if type(value) is not int or value<=0:raise ValueError("A positive integer identity is required")
    return value
def _expected(value):return None if value is None else _identifier(value)
def _snapshot(value):
    clean=_stored_snapshot(value)
    if len(clean["notes"])>64 or len(clean["categories"])>64 or len(clean["similarities"])>64:raise ValueError("Classification lists exceed the 64-entry bound")
    if set(clean["sensory"])-AXES:raise ValueError("Unknown sensory axis")
    if any(len(item["reason"])>500 for item in clean["similarities"]):raise ValueError("Relationship reason exceeds 500 characters")
    return clean

EMPTY={"description":"","notes":[],"categories":[],"sensory":{},"similarities":[]}

def _hash(value):return hashlib.sha256(_json(value).encode()).hexdigest()
def _content(value):return any(value.get(key) for key in SNAPSHOT_FIELDS)
def _safe(value):
    if isinstance(value,datetime):return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()
    if isinstance(value,dict):return {str(k):_safe(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [_safe(v) for v in value]
    return value
def _head(storage,connection,flavor_id):
    _identifier(flavor_id)
    if not connection.execute(select(storage.table("touchscreen_flavors").c.id).where(
            storage.table("touchscreen_flavors").c.id==flavor_id)).first():raise ValueError("Unknown flavor identity")
    current=storage._current(connection,flavor_id)
    return {"flavorId":flavor_id,"versionId":current["version_id"] if current else None,
        "snapshot":deepcopy(current["snapshot"] if current else EMPTY),
        "verified":bool(current and not current["is_baseline"] and _content(current["snapshot"]))}
def _refs(storage,connection,snapshot):
    categories=storage.table("touchscreen_flavor_categories");flavors=storage.table("touchscreen_flavors")
    ids=set(snapshot["categories"])
    if ids and set(connection.scalars(select(categories.c.id).where(categories.c.id.in_(ids))))!=ids:raise ValueError("Unknown category identity")
    related={row["flavor_id"] for row in snapshot["similarities"]}
    if related and set(connection.scalars(select(flavors.c.id).where(flavors.c.id.in_(related))))!=related:raise ValueError("Unknown similarity identity")
def _patch(base,patch):
    if not isinstance(patch,dict) or not patch or set(patch)-SNAPSHOT_FIELDS:raise ValueError("Only classification fields may change")
    merged=deepcopy(base)
    for key,value in patch.items():
        if key=="sensory":
            if not isinstance(value,dict):raise ValueError("Sensory changes must identify axes")
            if set(value)-AXES:raise ValueError("Unknown sensory axis")
            for axis,intensity in value.items():
                if intensity is None:merged["sensory"].pop(axis,None)
                else:merged["sensory"][axis]=intensity
        else:merged[key]=value
    return _snapshot(merged)
def _canonical_patch(base,patch):
    proposed=_patch(base,patch)
    canonical={key:proposed[key] for key in patch if key!="sensory"}
    if "sensory" in patch:canonical["sensory"]={axis:None if value is None else proposed["sensory"][axis] for axis,value in sorted(patch["sensory"].items())}
    return canonical
def _effective_patch(base,patch):
    """Fingerprint effects only; individual submissions retain supplied intent."""
    after=_patch(base,patch)
    effective={field:deepcopy(after[field]) for field in patch
        if field!="sensory" and after[field]!=base[field]}
    axes={axis:(float(after["sensory"][axis]) or 0.0) if axis in after["sensory"] else None
        for axis in patch.get("sensory",{})
        if base["sensory"].get(axis)!=after["sensory"].get(axis)}
    if axes:effective["sensory"]=axes
    # IDs stay integers; numeric intensity/weight representations are semantic.
    if "similarities" in effective:
        for relationship in effective["similarities"]:
            relationship["weight"]=float(relationship["weight"])
    return effective
def _diff(before,after):
    return [{"field":key,"before":before[key],"after":after[key]} for key in sorted(SNAPSHOT_FIELDS) if before[key]!=after[key]]
def _origin(storage,connection,flavor_id,actor_id):
    versions=storage.table("fi_classification_versions")
    found=connection.scalar(select(versions.c.id).where(versions.c.flavor_id==flavor_id,
        versions.c.is_baseline.is_(True),versions.c.restores_version_id.is_(None)))
    if found is not None:return found
    version,_=storage._version(connection,flavor_id=flavor_id,snapshot=deepcopy(EMPTY),
        reviewer_id=actor_id,reason="Captured original unclassified state; not a publication",is_baseline=True)
    return version
def _group(storage,connection,group_id,check_head=True):
    groups=storage.table("fi_suggestion_review_groups");versions=storage.table("fi_classification_versions")
    group=connection.execute(select(groups).where(groups.c.id==group_id)).mappings().one_or_none()
    if not group or group["status"]!="PENDING":raise ValueError("Review group is missing or closed")
    submissions=connection.execute(select(storage.table("fi_employee_suggestions")).where(
        storage.table("fi_employee_suggestions").c.review_group_id==group_id).order_by(
        storage.table("fi_employee_suggestions").c.id)).mappings().all()
    if not submissions:raise ValueError("Review group has no submissions")
    base=connection.execute(select(versions).where(versions.c.id==group["base_version_id"])).mappings().one()
    published=connection.scalar(select(func.count()).select_from(storage.table("fi_publication_revisions")).where(
        storage.table("fi_publication_revisions").c.version_id==base["id"]))
    expected=None if base["is_baseline"] and not published else base["id"]
    current=_head(storage,connection,group["flavor_id"])
    if check_head and current["versionId"]!=expected:raise ValueError("Published baseline changed; explicit rereview is required")
    return dict(group),[dict(row) for row in submissions],expected,current

def submit_suggestion(storage,*,flavor_id,employee_id,store_id,base_version_id,proposed_patch,comment=None):
    _identifier(store_id);_expected(base_version_id)
    if comment is not None and (not isinstance(comment,str) or len(comment)>2000):raise ValueError("Comment is too long")
    with storage._write() as connection:
        employee=storage._principal(connection,employee_id)
        if employee["role"]=="STORE" and employee["store_id"]!=store_id:raise PermissionError("Employee suggestion location must match the assigned store")
        store=connection.execute(select(storage.table("stores")).where(storage.table("stores").c.id==store_id)).mappings().one_or_none()
        if not store:raise ValueError("Unknown existing store")
        current=_head(storage,connection,flavor_id)
        if current["versionId"]!=base_version_id:raise ValueError("Suggestion baseline is stale")
        proposed=_patch(current["snapshot"],proposed_patch)
        if proposed==current["snapshot"]:raise ValueError("Suggestion contains no changes")
        patch=deepcopy(proposed_patch)
        _refs(storage,connection,proposed)
        baseline=base_version_id if base_version_id is not None else _origin(storage,connection,flavor_id,employee_id)
        groups=storage.table("fi_suggestion_review_groups");fingerprint=_hash(_effective_patch(current["snapshot"],patch))
        pending=(groups.c.flavor_id==flavor_id,groups.c.base_version_id==baseline,groups.c.status=="PENDING")
        group_id=connection.scalar(select(groups.c.id).where(*pending,groups.c.patch_fingerprint==fingerprint))
        if group_id is None:
            # Predecessor fingerprints included unchanged fields and numeric JSON
            # spelling. Reuse those groups without rewriting immutable history.
            submissions=storage.table("fi_employee_suggestions")
            first_ids=(select(func.min(submissions.c.id)).where(submissions.c.review_group_id==groups.c.id)
                .correlate(groups).scalar_subquery())
            candidates=connection.execute(select(groups.c.id,submissions.c.proposed_patch).select_from(
                groups.join(submissions,submissions.c.id==first_ids)).where(*pending).order_by(groups.c.id)).mappings()
            for candidate in candidates:
                if _hash(_effective_patch(current["snapshot"],candidate["proposed_patch"]))==fingerprint:
                    group_id=candidate["id"];break
        if group_id is None:group_id=connection.execute(insert(groups).values(flavor_id=flavor_id,
            base_version_id=baseline,patch_fingerprint=fingerprint,status="PENDING")).inserted_primary_key[0]
        submission=connection.execute(insert(storage.table("fi_employee_suggestions")).values(
            review_group_id=group_id,flavor_id=flavor_id,base_version_id=baseline,employee_id=employee_id,store_id=store_id,
            author_context={"username":employee["username"],"store_name":store["name"]},original_snapshot=current["snapshot"],
            proposed_patch=patch,comment=comment)).inserted_primary_key[0]
        return {"submission_id":submission,"review_group_id":group_id}

def _bulk(base,patch):
    if not isinstance(patch,dict) or not patch or set(patch)-SNAPSHOT_FIELDS:raise ValueError("Only classification fields may change")
    result=deepcopy(base)
    for field,value in patch.items():
        if field=="description":result[field]=value;continue
        if not isinstance(value,dict):raise ValueError("Bulk changes require add/remove or set/clear")
        if field in ("notes","categories"):
            if set(value)-{"add","remove"}:raise ValueError("Invalid set operation")
            if not isinstance(value.get("add",[]),list) or not isinstance(value.get("remove",[]),list):raise ValueError("Bulk set changes require lists")
            add,remove=set(value.get("add") or []),set(value.get("remove") or [])
            if add&remove:raise ValueError("The same value cannot be added and removed")
            result[field]=sorted((set(result[field])|add)-remove)
        elif field=="sensory":
            if set(value)-{"set","clear"}:raise ValueError("Invalid sensory operation")
            setting=value.get("set") or {};clearing=value.get("clear") or []
            if not isinstance(clearing,list) or set(clearing)-AXES:raise ValueError("Invalid sensory axes")
            if not isinstance(setting,dict) or set(setting)&set(clearing):raise ValueError("Conflicting sensory operation")
            result[field].update(setting)
            for axis in clearing:result[field].pop(axis,None)
        else:
            if set(value)-{"add","remove"}:raise ValueError("Invalid relationship operation")
            if not isinstance(value.get("add",[]),list) or not isinstance(value.get("remove",[]),list):raise ValueError("Relationship changes require lists")
            add=value.get("add") or [];remove={_identifier(value) for value in value.get("remove",[])}
            if not all(isinstance(row,dict) and "flavor_id" in row for row in add):raise ValueError("Invalid relationship")
            if any(row.get("flavor_id") in remove for row in add):raise ValueError("Conflicting relationship operation")
            relationships={row["flavor_id"]:row for row in result[field]}
            for related in remove:relationships.pop(related,None)
            for row in add:relationships[row["flavor_id"]]=row
            result[field]=list(relationships.values())
    return _snapshot(result)

def _merge(base,patches):
    # Merge independent changes by set member/axis/relationship identity. Two
    # different values for one atomic path are conflicting and remain separate.
    intents={}
    for patch in patches:
        after=_patch(base,patch)
        for field in patch:
            if field in ("notes","categories"):
                before_set,after_set=set(base[field]),set(after[field])
                changes={(field,value):value in after_set for value in before_set^after_set}
            elif field in ("sensory","similarities"):
                before_map=base[field] if field=="sensory" else {row["flavor_id"]:row for row in base[field]}
                after_map=after[field] if field=="sensory" else {row["flavor_id"]:row for row in after[field]}
                changes={(field,key):after_map.get(key) for key in before_map.keys()|after_map.keys() if before_map.get(key)!=after_map.get(key)}
            else:changes={(field,None):after[field]} if base[field]!=after[field] else {}
            for key,value in changes.items():
                if key in intents and intents[key]!=value:raise ValueError("Selected suggestions contain conflicting changes")
                intents[key]=value
    result=deepcopy(base)
    for (field,key),value in intents.items():
        if field in ("notes","categories"):
            values=set(result[field]);values.add(key) if value else values.discard(key);result[field]=sorted(values)
        elif field=="sensory":
            if value is None:result[field].pop(key,None)
            else:result[field][key]=value
        elif field=="similarities":
            relationships={row["flavor_id"]:row for row in result[field]}
            if value is None:relationships.pop(key,None)
            else:relationships[key]=value
            result[field]=list(relationships.values())
        else:result[field]=value
    return _snapshot(result)

class FlavorAdministration:
    def __init__(self,storage,clock=None):self.storage=storage;self.clock=clock or _now
    def _plan(self,actor_id,kind,reason,changes,groups=None,source=None):
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:raise ValueError("An audit reason is required")
        plan={"id":str(uuid.uuid4()),"actorId":actor_id,"kind":kind,"reason":reason.strip(),
            "expiresAt":(self.clock()+timedelta(minutes=10)).isoformat(),"changes":changes,
            "reviewGroups":groups or [],"sourceOperationId":source}
        plan["fingerprint"]=_hash(plan);return plan
    def _change(self,current,after,restore=None):
        if any(item["flavor_id"]==current["flavorId"] for item in after["similarities"]):
            raise ValueError("A product cannot recommend itself")
        return {"flavorId":current["flavorId"],"beforeVersionId":current["versionId"],
            "before":current["snapshot"],"after":after,"beforeVerified":current["verified"],
            "afterVerified":bool(_content(after)),"restoreVersionId":restore,
            "changed":restore is not None or current["snapshot"]!=after or current["verified"]!=bool(_content(after)),"differences":_diff(current["snapshot"],after)}
    def preview_publish(self,*,actor_id,flavor_id,snapshot,expected_version_id,reason):
        _expected(expected_version_id)
        after=_snapshot(snapshot)
        with self.storage.engine.connect() as connection:
            self.storage._principal(connection,actor_id,admin=True);current=_head(self.storage,connection,flavor_id)
            if current["versionId"]!=expected_version_id:raise ValueError("Published baseline changed")
            _refs(self.storage,connection,after)
        changes=[self._change(current,after)]
        return self._plan(actor_id,"publish",reason,changes)
    def preview_bulk(self,*,actor_id,flavor_ids,patch,expected_versions,reason):
        ids=sorted({_identifier(value) for value in flavor_ids})
        if not isinstance(expected_versions,dict):raise ValueError("Expected baseline mapping required")
        if not ids or len(ids)>500:raise ValueError("Select one to 500 products")
        changes=[]
        with self.storage.engine.connect() as connection:
            self.storage._principal(connection,actor_id,admin=True)
            for flavor_id in ids:
                if flavor_id not in expected_versions and str(flavor_id) not in expected_versions:raise ValueError("Every selected product needs an expected baseline")
                expected=_expected(expected_versions.get(flavor_id,expected_versions.get(str(flavor_id))))
                current=_head(self.storage,connection,flavor_id)
                if current["versionId"]!=expected:raise ValueError("Published baseline changed")
                after=_bulk(current["snapshot"],patch);_refs(self.storage,connection,after)
                changes.append(self._change(current,after))
        return self._plan(actor_id,"bulk",reason,changes)
    def preview_restore(self,*,actor_id,flavor_id,restores_version_id,expected_version_id,reason):
        _identifier(restores_version_id);_expected(expected_version_id)
        versions=self.storage.table("fi_classification_versions");publications=self.storage.table("fi_publication_revisions")
        with self.storage.engine.connect() as connection:
            self.storage._principal(connection,actor_id,admin=True);current=_head(self.storage,connection,flavor_id)
            if current["versionId"]!=expected_version_id:raise ValueError("Published baseline changed")
            old=connection.execute(select(versions).where(versions.c.id==restores_version_id,versions.c.flavor_id==flavor_id)).mappings().one_or_none()
            if not old or (not old["is_baseline"] and not connection.scalar(select(func.count()).select_from(publications).where(publications.c.version_id==old["id"]))):raise ValueError("Only a recorded published or original unclassified state may be restored")
        return self._plan(actor_id,"restore",reason,[self._change(current,old["snapshot"],old["id"])])
    def preview_reverse(self,*,actor_id,operation_id,reason):
        items=self.storage.table("fi_administration_operation_items");operations=self.storage.table("fi_administration_operations")
        with self.storage.engine.connect() as connection:
            self.storage._principal(connection,actor_id,admin=True)
            if not connection.execute(select(operations.c.id).where(operations.c.id==operation_id)).first():raise ValueError("Unknown operation")
            rows=connection.execute(select(items).where(items.c.operation_id==operation_id)).mappings().all()
            if not rows:raise ValueError("This operation has no published changes to reverse")
            changes=[]
            for row in rows:
                current=_head(self.storage,connection,row["flavor_id"])
                if current["versionId"]!=row["after_version_id"]:raise ValueError("A product changed after the operation; reversal requires explicit rereview")
                changes.append(self._change(current,row["before_snapshot"],row["restore_version_id"]))
        return self._plan(actor_id,"reverse",reason,changes,source=operation_id)
    def preview_review(self,*,actor_id,group_ids,decision,reason,modified_patches=None):
        if decision not in {"APPROVED","REJECTED","MODIFIED_APPROVED"}:raise ValueError("Invalid review decision")
        if (decision=="MODIFIED_APPROVED")!=(modified_patches is not None):raise ValueError("Only modify-and-approve accepts replacement patches")
        ids=sorted({_identifier(value) for value in group_ids})
        if not ids or len(ids)>500:raise ValueError("Select one to 500 review groups")
        groups=[];by_flavor=defaultdict(list);currents={}
        with self.storage.engine.connect() as connection:
            self.storage._principal(connection,actor_id,admin=True)
            for group_id in ids:
                group,submissions,expected,current=_group(self.storage,connection,group_id,decision!="REJECTED")
                patch=(modified_patches.get(group_id,modified_patches.get(str(group_id))) if modified_patches is not None else submissions[0]["proposed_patch"])
                if decision!="REJECTED":patch=_canonical_patch(current["snapshot"],patch)
                groups.append({"id":group_id,"flavorId":group["flavor_id"],"decision":decision,
                    "baseVersionId":group["base_version_id"],"expectedVersionId":current["versionId"],"patch":patch})
                if decision!="REJECTED":by_flavor[group["flavor_id"]].append(patch);currents[group["flavor_id"]]=current
            changes=[]
            for flavor_id,patches in by_flavor.items():
                after=_merge(currents[flavor_id]["snapshot"],patches);_refs(self.storage,connection,after)
                change=self._change(currents[flavor_id],after);change["changed"]=True;changes.append(change)
        return self._plan(actor_id,"review",reason,changes,groups)
    def commit(self,*,actor_id,plan):
        if not isinstance(plan,dict) or plan.get("actorId")!=actor_id:raise PermissionError("Preview belongs to another administrator")
        signed={key:value for key,value in plan.items() if key!="fingerprint"}
        if plan.get("fingerprint")!=_hash(signed):raise ValueError("Preview changed; prepare it again")
        expires=datetime.fromisoformat(plan["expiresAt"])
        if expires.tzinfo is None or self.clock()>=expires:raise ValueError("Preview expired; prepare it again")
        if not any(change.get("changed",True) for change in plan["changes"]) and not plan["reviewGroups"]:raise ValueError("Preview contains no changes")
        storage=self.storage;operations=storage.table("fi_administration_operations");items=storage.table("fi_administration_operation_items")
        with storage._write() as connection:
            storage._principal(connection,actor_id,admin=True)
            existing=connection.execute(select(operations).where(operations.c.preview_id==plan["id"])).mappings().one_or_none()
            if existing:
                if existing["actor_id"]!=actor_id or existing["plan_fingerprint"]!=plan["fingerprint"]:raise ValueError("Preview was already used")
                return self._receipt(connection,existing["id"],True)
            for change in plan["changes"]:
                current=_head(storage,connection,change["flavorId"])
                if current["versionId"]!=change["beforeVersionId"] or current["snapshot"]!=change["before"]:raise ValueError("Published baseline changed; the entire operation was cancelled")
                _refs(storage,connection,_snapshot(change["after"]))
            for group in plan["reviewGroups"]:
                _group(storage,connection,group["id"],group["decision"]!="REJECTED")
            operation_id=str(uuid.uuid4())
            connection.execute(insert(operations).values(id=operation_id,preview_id=plan["id"],actor_id=actor_id,
                kind=plan["kind"],reason=plan["reason"],source_operation_id=plan["sourceOperationId"],plan_fingerprint=plan["fingerprint"]))
            created={}
            for change in plan["changes"]:
                if not change.get("changed",True):continue
                flavor_id=change["flavorId"]
                restore_before=change["beforeVersionId"] or _origin(storage,connection,flavor_id,actor_id)
                restoring=change["restoreVersionId"]
                is_baseline=not _content(change["after"])
                if is_baseline and restoring is None:restoring=_origin(storage,connection,flavor_id,actor_id)
                version,revision=storage._version(connection,flavor_id=flavor_id,snapshot=change["after"],
                    reviewer_id=actor_id,reason=plan["reason"],restores_version_id=restoring,is_baseline=is_baseline)
                created[flavor_id]=(version,revision)
                connection.execute(insert(items).values(operation_id=operation_id,flavor_id=flavor_id,
                    before_version_id=change["beforeVersionId"],restore_version_id=restore_before,after_version_id=version,
                    before_snapshot=change["before"],after_snapshot=change["after"],
                    before_verified=change["beforeVerified"],after_verified=change["afterVerified"]))
            decisions=defaultdict(list)
            for review in plan["reviewGroups"]:
                group,submissions,expected,current=_group(storage,connection,review["id"],False)
                version=created.get(group["flavor_id"],(None,None))[0] if review["decision"]!="REJECTED" else None
                after=next((change["after"] for change in plan["changes"] if change["flavorId"]==group["flavor_id"]),None) if version else None
                decision_id=connection.execute(insert(storage.table("fi_review_decisions")).values(
                    review_group_id=group["id"],flavor_id=group["flavor_id"],base_version_id=group["base_version_id"],
                    decision=review["decision"],reviewer_id=actor_id,published_version_id=version,
                    proposed_patch_snapshot=_patch(submissions[0]["original_snapshot"],submissions[0]["proposed_patch"]),
                    published_snapshot=after,reason=plan["reason"])).inserted_primary_key[0]
                decisions[group["flavor_id"]].append(decision_id)
                connection.execute(insert(storage.table("fi_administration_operation_reviews")).values(
                    operation_id=operation_id,review_group_id=group["id"],decision_id=decision_id))
                connection.execute(update(storage.table("fi_suggestion_review_groups")).where(
                    storage.table("fi_suggestion_review_groups").c.id==group["id"]).values(status=review["decision"]))
            for flavor_id,(version,revision) in created.items():
                action="ROLLBACK" if plan["kind"] in {"restore","reverse"} else "PUBLISH" if plan["kind"]=="review" else "DIRECT_EDIT"
                storage._publication(connection,flavor_id=flavor_id,version_id=version,revision=revision,
                    reviewer_id=actor_id,reason=plan["reason"],action=action,decision_id=decisions[flavor_id][0] if decisions[flavor_id] else None)
            return self._receipt(connection,operation_id)
    def _receipt(self,connection,operation_id,replayed=False):
        operations=self.storage.table("fi_administration_operations");items=self.storage.table("fi_administration_operation_items")
        operation=dict(connection.execute(select(operations).where(operations.c.id==operation_id)).mappings().one())
        changes=[{"flavorId":row["flavor_id"],"beforeVersionId":row["before_version_id"],
            "afterVersionId":row["after_version_id"],"before":row["before_snapshot"],"after":row["after_snapshot"]}
            for row in connection.execute(select(items).where(items.c.operation_id==operation_id)).mappings()]
        return _safe({"id":operation_id,"kind":operation["kind"],"actorId":operation["actor_id"],
            "createdAt":operation["created_at"],"changes":changes,"replayed":replayed})
    def catalog_state(self,variation_ids,*,actor_id):
        storage=self.storage;links=storage.table("fi_square_flavor_links")
        by_variation={};profiles={}
        with storage.engine.connect() as connection:
            storage._principal(connection,actor_id)
            ids=list(dict.fromkeys(variation_ids))
            for offset in range(0,len(ids),800):
                for row in connection.execute(select(links).where(links.c.square_variation_id.in_(ids[offset:offset+800]))).mappings():by_variation[row["square_variation_id"]]=row["flavor_id"]
            for flavor_id in set(by_variation.values()):profiles[str(flavor_id)]=_head(storage,connection,flavor_id)
            taxonomy={"categories":[dict(row) for row in connection.execute(select(storage.table("touchscreen_flavor_categories"))).mappings()],
                      "notes":[dict(row) for row in connection.execute(select(storage.table("fi_notes"))).mappings()]}
        return _safe({"byVariation":by_variation,"profiles":profiles,"taxonomy":taxonomy})
    def history(self,*,actor_id,flavor_id):
        storage=self.storage
        with storage.engine.connect() as connection:
            storage._principal(connection,actor_id,admin=True);_head(storage,connection,flavor_id)
            result={"flavorId":flavor_id}
            for key,table_name in (("versions","fi_classification_versions"),("publications","fi_publication_revisions"),("audit","fi_audit_events"),("operations","fi_administration_operation_items")):
                table=storage.table(table_name)
                result[key]=[dict(row) for row in connection.execute(select(table).where(table.c.flavor_id==flavor_id)).mappings()]
        return _safe(result)
    def queue(self,*,actor_id,status="PENDING"):
        storage=self.storage;groups=storage.table("fi_suggestion_review_groups")
        if status not in {"PENDING","APPROVED","REJECTED","MODIFIED_APPROVED","ALL"}:raise ValueError("Unknown review status")
        with storage.engine.connect() as connection:
            storage._principal(connection,actor_id,admin=True)
            statement=select(groups).order_by(groups.c.id.desc())
            if status!="ALL":statement=statement.where(groups.c.status==status)
            result=[]
            for group in connection.execute(statement).mappings():
                submissions=[dict(row) for row in connection.execute(select(storage.table("fi_employee_suggestions")).where(
                    storage.table("fi_employee_suggestions").c.review_group_id==group["id"]).order_by(storage.table("fi_employee_suggestions").c.id)).mappings()]
                current=_head(storage,connection,group["flavor_id"]);first=submissions[0];original=first["original_snapshot"]
                versions=storage.table("fi_classification_versions")
                base=connection.execute(select(versions).where(versions.c.id==group["base_version_id"])).mappings().one()
                was_published=connection.scalar(select(func.count()).select_from(storage.table("fi_publication_revisions")).where(storage.table("fi_publication_revisions").c.version_id==base["id"]))
                expected=None if base["is_baseline"] and not was_published else base["id"]
                proposed=_patch(original,first["proposed_patch"])
                result.append({"id":group["id"],"flavorId":group["flavor_id"],"status":group["status"],
                    "baseVersionId":expected,"currentVersionId":current["versionId"],"stale":expected!=current["versionId"],
                    "original":original,"proposed":proposed,"differences":_diff(original,proposed),
                    "submissions":[{"id":row["id"],"employeeId":row["employee_id"],"storeId":row["store_id"],
                        "username":row["author_context"]["username"],"storeName":row["author_context"]["store_name"],
                        "submittedAt":row["submitted_at"],"proposedPatch":row["proposed_patch"],"comment":row["comment"]} for row in submissions]})
        return _safe({"groups":result})
    def my_suggestions(self,*,employee_id):
        storage=self.storage;submissions=storage.table("fi_employee_suggestions");groups=storage.table("fi_suggestion_review_groups")
        with storage.engine.connect() as connection:
            storage._principal(connection,employee_id)
            result=[dict(row) for row in connection.execute(select(submissions,groups.c.status).join(groups,groups.c.id==submissions.c.review_group_id).where(
                submissions.c.employee_id==employee_id).order_by(submissions.c.id.desc())).mappings()]
        return _safe({"submissions":result})
    def analytics(self,*,actor_id,flavor_ids=None):
        storage=self.storage
        with storage.engine.connect() as connection:
            storage._principal(connection,actor_id,admin=True)
            groups=[dict(row) for row in connection.execute(select(storage.table("fi_suggestion_review_groups"))).mappings()]
            submissions=[dict(row) for row in connection.execute(select(storage.table("fi_employee_suggestions"))).mappings()]
            operations=[dict(row) for row in connection.execute(select(storage.table("fi_administration_operations")).order_by(storage.table("fi_administration_operations").c.created_at.desc())).mappings()]
            operation_counts=dict(connection.execute(select(storage.table("fi_administration_operation_items").c.operation_id,
                func.count()).group_by(storage.table("fi_administration_operation_items").c.operation_id)).all())
            publications=[dict(row) for row in connection.execute(select(storage.table("fi_publication_revisions")).order_by(
                storage.table("fi_publication_revisions").c.id.desc())).mappings()]
            ids=set(flavor_ids) if flavor_ids is not None else set(connection.scalars(select(storage.table("touchscreen_flavors").c.id)))
            verified=sum(_head(storage,connection,identity)["verified"] for identity in ids)
        statuses={row["id"]:row["status"] for row in groups};counts=Counter(statuses.values())
        outcomes=Counter(statuses[row["review_group_id"]] for row in submissions)
        approved=outcomes["APPROVED"]+outcomes["MODIFIED_APPROVED"];reviewed=approved+outcomes["REJECTED"]
        people=defaultdict(list)
        for row in submissions:people[row["employee_id"]].append(row["id"])
        corrections=Counter(row["flavor_id"] for row in publications)
        pending_flavors={row["flavor_id"] for row in groups if row["status"]=="PENDING"}
        return _safe({"counts":{"pendingGroups":counts["PENDING"],"pendingSubmissions":outcomes["PENDING"],
            "approvedSubmissions":approved,"rejectedSubmissions":outcomes["REJECTED"],"reviewedSubmissions":reviewed,
            "decidedSubmissions":reviewed,"approvalRate":approved/reviewed if reviewed else None,
            "verifiedProducts":verified,"totalProducts":len(ids)},
            "approvalRate":approved/reviewed if reviewed else None,
            "submissionFrequency":dict(Counter(str(row["submitted_at"])[:10] for row in submissions)),
            "employeeParticipation":[{"employeeId":person,"submissionIds":rows,"count":len(rows),
                "approved":sum(statuses[row["review_group_id"]] in {"APPROVED","MODIFIED_APPROVED"} for row in submissions if row["employee_id"]==person),
                "stores":sorted({row["store_id"] for row in submissions if row["employee_id"]==person})} for person,rows in people.items()],
            "repeatedCorrections":[{"flavorId":flavor,"publicationCount":count} for flavor,count in corrections.items() if count>1],
            "coverage":{"total":len(ids),"verified":verified,"unpublished":len(ids)-verified,
                        "pendingReview":len(ids&pending_flavors)},
            "recentChanges":publications[:30],
            "operations":[{"id":row["id"],"kind":row["kind"],"actorId":row["actor_id"],
                           "createdAt":row["created_at"],"productCount":operation_counts.get(row["id"],0)} for row in operations[:50]]})
