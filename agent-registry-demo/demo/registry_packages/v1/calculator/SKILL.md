---
name: calculator
description: Precision arithmetic calculation skill for addition and multiplication.
allowed-tools: add multiply
metadata:
  version: "1.0.0"
  adk_additional_tools:
    - add
    - multiply
---
# Calculator Skill (v1)

## Overview
This skill provides verified, deterministic arithmetic operations using the registered remote Calculator tool service.

## Directives & Execution Rules
1. For any arithmetic calculation, you MUST call the registered calculator tools (`add`, `multiply`). Do NOT perform mental arithmetic, approximation, or in-context calculation.
2. For compound calculations (e.g., multiply followed by addition), sequence your tool invocations logically: execute multiplication first, then add the result.
3. You MUST format final outputs strictly as:
   "Result: <value> (Computed via <tools_used>)"
   Example:
   "Result: 70 (Computed via multiply, add)"
