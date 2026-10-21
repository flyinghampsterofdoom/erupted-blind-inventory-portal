"""Shared configured ownership rule, independent of funding and quantities."""


def single_default_mapping(mappings):
    """A variation has one owner only when exactly one active mapping is default.

    Callers supply active mappings belonging to active vendors. Multiple default
    paths, even for one vendor, require resolution rather than an arbitrary cost.
    """
    defaults = [mapping for mapping in mappings if mapping.is_default_vendor]
    return defaults[0] if len(defaults) == 1 else None
