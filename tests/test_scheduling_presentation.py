from app.v2.scheduling_presentation import board_status, continuity_tone


def test_status_uses_publication_state_not_approval():
    board={'period':None,'shifts':[]}
    assert board_status(board)['tone']=='neutral'
    board['period']={'status':'DRAFT','approval':{'version':1}}
    assert board_status(board)['tone']=='neutral'
    board['period']['generated_at']='2026-10-20'
    assert board_status(board)['tone']=='draft'
    board['current_published_period_id']=5
    assert board_status(board)['tone']=='mixed'
    board['period']['status']='PUBLISHED'
    assert board_status(board)['tone']=='published'
    board['period']['status']='ARCHIVED';board['mode_label']='Archived'
    assert board_status(board)=={'tone':'neutral','label':'Archived'}


def test_unpublished_is_not_an_error():
    assert continuity_tone({'label':'Next operating week unpublished','color':'red'})=='draft'
    assert continuity_tone({'label':'Publication blocked','color':'red'})=='blocked'
