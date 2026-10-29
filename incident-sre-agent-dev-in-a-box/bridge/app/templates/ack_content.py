"""Single-sourced acknowledgment and failure note content for ticket integrations."""

INVESTIGATION_TITLE = "Gemini Cloud Assist investigation"
ACK_SUBTITLE = "Investigation started"
ACK_STATUS_ITALIC = "Automated analysis is running now."
WHAT_HAPPENS_NEXT_TITLE = "What happens next"

FAILURE_SUBTITLE = "Investigation failed"
FAILURE_MESSAGE_TEMPLATE = "Investigation could not be completed. Error: {error}"


def get_next_steps(ticket_entry_noun: str) -> list[str]:
    """Generate standardized next steps list using the adapter-specific ticket entry noun.

    For ServiceNow, ticket_entry_noun is 'work note'.
    For Jira, ticket_entry_noun is 'comment'.
    """
    return [
        (
            "Gemini Cloud Assist is analysing this incident against the project's "
            "resources, recent changes and logs."
        ),
        (f"The findings are posted to this ticket automatically, as a second {ticket_entry_noun}."),
        "This usually takes 3 to 6 minutes. No action is needed from you in the meantime.",
    ]


def get_failure_message(error: str) -> str:
    """Format standardized investigation failure message."""
    return FAILURE_MESSAGE_TEMPLATE.format(error=error)
