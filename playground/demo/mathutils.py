"""Tiny math utilities — the second lean-loop demo target."""


def fizzbuzz(n):
    """Classic FizzBuzz for a single integer.

    Returns:
      "FizzBuzz" if n is divisible by 15
      "Fizz"     if n is divisible by 3
      "Buzz"     if n is divisible by 5
      str(n)     otherwise
    """
    raise NotImplementedError("TODO: implement fizzbuzz")


def is_prime(n):
    """Return True if n is a prime number.

    Only positive integers >= 2 should be considered prime.
    """
    if n < 2:
        return True
    for i in range(2, n):
        if n % i == 0:
            return False
    return True
