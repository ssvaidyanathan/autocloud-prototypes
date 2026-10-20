# Arithmetic Reference Guide (v2)

## Tool Invocation Conventions
- Use `multiply(a, b)` for product operations.
- Use `add(a, b)` for summation operations.
- Use `power(base, exponent)` for exponentiation operations.
- Boundary conditions:
  - Base `0.0` with negative exponent is invalid (division by zero).
  - Negative base with non-integer exponent produces complex numbers and is rejected.
