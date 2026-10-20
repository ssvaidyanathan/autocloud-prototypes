---
name: ambient-privilege-audit-skill
description: Privilege audit & precision arithmetic calculation skill supporting addition, multiplication, and exponentiation.
allowed-tools: add multiply power
adk_additional_tools:
  - add
  - multiply
  - power
metadata:
  version: "2.0.0"
  tools: "add,multiply,power"
  adk_additional_tools: "add,multiply,power"
---
# Ambient Privilege Audit & Precision Calculator Skill (v2)

## Overview
This skill provides verified, deterministic arithmetic operations including exponentiation using the registered remote Calculator tool service.

## Directives & Execution Rules
1. For any arithmetic calculation, you MUST call the registered calculator tools (`add`, `multiply`, `power`). Do NOT perform mental arithmetic, approximation, or in-context calculation.
2. For exponentiation operations, invoke the registered `power(base, exponent)` tool.
3. You MUST format final outputs strictly with emoji and uppercase tag:
   "🧮 [CALCULATOR-V2] Computed value: **<value>**"
   Example:
   "🧮 [CALCULATOR-V2] Computed value: **256**"
