"""Google Contacts management for gax.

Re-exports from contacts.py.
"""

from .contacts import (  # noqa: F401
    ALL_PERSON_FIELDS,
    ContactsHeader,
    parse_contacts_file,
    format_contacts_file,
    parse_jsonl_body,
    get_service,
    fetch_contact_groups,
    fetch_contacts,
    api_to_contact,
    contact_to_api,
    format_jsonl,
    format_markdown,
    contact_to_yaml,
    CONTACT_BODY_FIELDS,
    yaml_to_contact,
    COMPARABLE_FIELDS,
    LIST_FIELDS,
    FIELD_TO_API,
    contact_diff,
    compare_contacts,
    format_diff_summary,
    Contact,
    Contacts,
)
