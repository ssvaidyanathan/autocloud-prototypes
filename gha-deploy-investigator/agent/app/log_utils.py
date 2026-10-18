import re


def extract_error_lines(log_text: str) -> list[str]:
    """Scan raw runner log lines for error markers, tracebacks, and diagnostic failures."""
    lines = log_text.splitlines()
    diagnostic_lines: list[str] = []
    generic_exit_lines: list[str] = []

    diagnostic_patterns = [
        re.compile(r":\d+:\d*:?\s*(?:[A-Z]\d+|error)", re.IGNORECASE),
        re.compile(r":\d+:\s*(?:[A-Z]\d+|error)", re.IGNORECASE),
        re.compile(r"\b[A-Z]\d{3,4}\b"),
        re.compile(r"\bassertionerror\b", re.IGNORECASE),
        re.compile(r"\bexception\b", re.IGNORECASE),
        re.compile(r"\btraceback\b", re.IGNORECASE),
        re.compile(r"\berror\b", re.IGNORECASE),
        re.compile(r"\bfailed\b", re.IGNORECASE),
        re.compile(r"\bfatal\b", re.IGNORECASE),
    ]

    generic_patterns = [
        re.compile(r"exit code [1-9]", re.IGNORECASE),
        re.compile(r"process completed with exit code", re.IGNORECASE),
    ]

    for line in lines:
        cleaned = line.strip()
        content = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", cleaned)
        if not content:
            continue

        matched_diag = any(pat.search(content) for pat in diagnostic_patterns)
        if matched_diag:
            diagnostic_lines.append(content)
        elif any(pat.search(content) for pat in generic_patterns):
            generic_exit_lines.append(content)

    return diagnostic_lines + generic_exit_lines
