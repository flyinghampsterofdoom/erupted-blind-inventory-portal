from sqlalchemy import select
from app import models as m
from app.services.access_control_service import permission_defs, permission_sections
from test_v2_primary import primary, login, post
from test_v2_scheduling_foundation import scheduling_db


def snapshot(Session):
    with Session() as db:
        return ([(r.id,r.role.value,r.permission_key,r.allowed) for r in db.scalars(select(m.RolePermissionOverride).order_by(m.RolePermissionOverride.id))],
                [(r.id,r.principal_id,r.permission_key,r.allowed) for r in db.scalars(select(m.PrincipalPermissionOverride).order_by(m.PrincipalPermissionOverride.id))])


def test_directory_editors_preserve_settings_and_group_all_permissions(primary):
    pid=primary.account('ADMIN',False);login(primary)
    with primary.Session() as db:
        db.add(m.RolePermissionOverride(role=m.PrincipalRole.LEAD,permission_key='scheduling.publish',allowed=False))
        db.add(m.PrincipalPermissionOverride(principal_id=pid,permission_key='ordering.manage',allowed=False));db.commit()
    before=snapshot(primary.Session)
    page=primary.client.get('/management/access-controls')
    assert page.status_code==200 and '<table' not in page.text
    assert 'Search employees or users' in page.text and 'Administrator' in page.text
    for path in ('/management/access-controls/roles/LEAD',f'/management/access-controls/principals/{pid}'):
        editor=primary.client.get(path)
        assert editor.status_code==200 and '<table' not in editor.text
        assert 'Action permissions' in editor.text and 'Navigation visibility' in editor.text
    assert snapshot(primary.Session)==before
    grouped=[p.key for section in permission_sections() for group in section['groups'] for p in group['permissions']]
    assert len(grouped)==len(set(grouped))==len(permission_defs())
    assert set(grouped)=={p.key for p in permission_defs()}


def test_targeted_role_save_does_not_clear_other_roles(primary):
    primary.account('ADMIN',False);login(primary)
    with primary.Session() as db:
        db.add(m.RolePermissionOverride(role=m.PrincipalRole.LEAD,permission_key='scheduling.publish',allowed=True));db.commit()
    response=post(primary.client,'/management/access-controls/roles/save',role='STORE',**{'role_perm__STORE__store.access':'on'})
    assert response.status_code==303
    with primary.Session() as db:
        assert db.scalar(select(m.RolePermissionOverride.allowed).where(m.RolePermissionOverride.role==m.PrincipalRole.LEAD,m.RolePermissionOverride.permission_key=='scheduling.publish')) is True
        assert db.scalar(select(m.RolePermissionOverride.allowed).where(m.RolePermissionOverride.role==m.PrincipalRole.STORE,m.RolePermissionOverride.permission_key=='store.access')) is True


def test_individual_allow_deny_reset_only_selected_user(primary):
    pid=primary.account('ADMIN',False);login(primary)
    other=primary.ids['manager']
    with primary.Session() as db:
        db.add(m.PrincipalPermissionOverride(principal_id=other,permission_key='ordering.manage',allowed=False));db.commit()
    assert post(primary.client,'/management/access-controls/principals/save',principal_id=str(pid),**{f'principal_perm__{pid}__ordering.manage':'DENY',f'principal_perm__{pid}__scheduling.publish':'ALLOW'}).status_code==303
    with primary.Session() as db:
        assert {(r.permission_key,r.allowed) for r in db.scalars(select(m.PrincipalPermissionOverride).where(m.PrincipalPermissionOverride.principal_id==pid))}=={('ordering.manage',False),('scheduling.publish',True)}
    assert post(primary.client,'/management/access-controls/principals/save',principal_id=str(pid),reset_permissions='1').status_code==303
    with primary.Session() as db:
        assert not list(db.scalars(select(m.PrincipalPermissionOverride).where(m.PrincipalPermissionOverride.principal_id==pid)))
        assert db.scalar(select(m.PrincipalPermissionOverride.allowed).where(m.PrincipalPermissionOverride.principal_id==other)) is False


def test_employee_cannot_escalate_even_with_management_users_override(primary):
    pid=primary.account();login(primary)
    with primary.Session() as db:
        db.add(m.PrincipalPermissionOverride(principal_id=pid,permission_key='management.users',allowed=True));db.commit()
    before=snapshot(primary.Session)
    for path in ('/management/access-controls','/management/access-controls/roles/ADMIN',f'/management/access-controls/principals/{pid}','/management/access-controls/roles/ADMIN/categories'):
        assert primary.client.get(path).status_code==403
    for path in ('/management/access-controls/roles/save','/management/access-controls/principals/save','/management/access-controls/roles/ADMIN/categories/save'):
        assert post(primary.client,path,principal_id=str(pid),**{f'principal_perm__{pid}__scheduling.publish':'ALLOW'}).status_code==403
    assert snapshot(primary.Session)==before


def test_polished_pages_keep_one_primary_heading_and_effective_permission_text(primary):
    pid=primary.account('ADMIN',False);login(primary)
    landing=primary.client.get('/management/access-controls')
    assert landing.text.count('<h1>')==1
    assert 'access-panel--directory' in landing.text
    assert landing.text.count('v2-button v2-button--secondary access-action')>=4
    for role in ('ADMIN','MANAGER','LEAD','STORE'):
        editor=primary.client.get('/management/access-controls/roles/'+role)
        assert editor.status_code==200 and editor.text.count('<h1>')==1
        assert 'access-editor-heading' in editor.text
    person=primary.client.get(f'/management/access-controls/principals/{pid}')
    assert 'Effective: Allowed' in person.text and 'Inherited from role' in person.text
