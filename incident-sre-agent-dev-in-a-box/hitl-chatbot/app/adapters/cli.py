"""Interactive CLI adapter for HITL Chatbot."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from app.agent import ChatbotAgentRunner

logger = logging.getLogger(__name__)


class CliAdapter:
    """Adapter facilitating interactive command-line interactions with HITL confirmation gates."""

    def __init__(
        self,
        runner: Optional[ChatbotAgentRunner] = None,
        input_func: Callable[[str], str] = input,
        output_func: Callable[[str], None] = print,
    ) -> None:
        self.runner = runner or ChatbotAgentRunner()
        self.input_func = input_func
        self.output_func = output_func

    def run_query(
        self,
        query: str,
        session_id: str = "cli-session",
        auto_confirm: bool = False,
        user_id: Optional[str] = None,
        approver_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a user query, prompting for HITL confirmation when required."""
        self.output_func(f"\n[USER QUERY] > {query}\n")

        # Step 1: Process initial turn
        turn_result = self.runner.process_turn(
            user_message=query, session_id=session_id, user_id=user_id
        )
        self.output_func(turn_result["text"])

        # If turn resulted in a draft ticket or comment, trigger HITL confirmation gate
        if turn_result.get("type") in ("draft_ticket", "draft_comment"):
            signed_token = turn_result["signed_token"]
            is_comment = turn_result.get("type") == "draft_comment"
            action_label = "comment addition" if is_comment else "ticket creation"
            prompt_str = (
                "\nApprove and add comment to Jira issue? [Y/n]: "
                if is_comment
                else "\nApprove and create Jira issue? [Y/n]: "
            )

            if auto_confirm:
                self.output_func(f"\n[AUTO-CONFIRM] Confirming {action_label} [Y]...")
                confirm_response = "y"
            else:
                try:
                    confirm_response = (
                        self.input_func(prompt_str).strip().lower()
                    )
                except (EOFError, KeyboardInterrupt):
                    confirm_response = "n"

            if confirm_response in ("y", "yes", "confirm", ""):
                self.output_func(
                    f"\n[HITL APPROVED] Executing Jira {action_label} with signed token...\n"
                )
                effective_approver = approver_id if approver_id is not None else user_id
                creation_result = self.runner.execute_approval(
                    signed_token, session_id=session_id, approver_id=effective_approver
                )
                self.output_func(creation_result["text"])
                return {
                    "turn_1": turn_result,
                    "turn_2": creation_result,
                    "status": "approved_and_created",
                }
            else:
                self.output_func(
                    f"\n[HITL CANCELLED] Jira {action_label} was cancelled by the user. "
                    "Zero side-effects created.\n"
                )
                return {
                    "turn_1": turn_result,
                    "status": "cancelled",
                }

        return {"turn_1": turn_result, "status": "completed"}
