"""Display semantics only; persisted lifecycle/status definitions are unchanged."""
def board_status(board):
    period = board.get('period')
    if not period:
        return {'tone': 'neutral', 'label': 'Not generated'}
    if period.get('is_historical') or period.get('status') == 'ARCHIVED':
        return {'tone': 'neutral', 'label': board['mode_label']}
    if period['status'] == 'PUBLISHED':
        return {'tone': 'published', 'label': 'Published'}
    if board.get('current_published_period_id'):
        return {'tone': 'mixed', 'label': 'Published + unpublished revision'}
    if not period.get('generated_at') and not board.get('shifts'):
        return {'tone': 'neutral', 'label': 'Not generated · Empty draft'}
    return {'tone': 'draft', 'label': 'Draft · Unpublished'}


def continuity_tone(continuity):
    # The legacy continuity service uses red for ordinary next-week unpublished.
    # Keep its business decision/label intact and correct its presentation only.
    if continuity['label'] == 'Next operating week unpublished':
        return 'draft'
    return {'green': 'published', 'yellow': 'draft', 'red': 'blocked'}.get(continuity['color'], 'neutral')
