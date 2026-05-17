from mathutils import fizzbuzz, is_prime


def test_fizzbuzz_number():
    assert fizzbuzz(1) == "1"


def test_fizzbuzz_three():
    assert fizzbuzz(3) == "Fizz"


def test_fizzbuzz_five():
    assert fizzbuzz(5) == "Buzz"


def test_fizzbuzz_fifteen():
    assert fizzbuzz(15) == "FizzBuzz"


def test_is_prime_two():
    assert is_prime(2) is True


def test_is_prime_seven():
    assert is_prime(7) is True


def test_is_prime_nine():
    assert is_prime(9) is False


def test_is_prime_one_is_not_prime():
    assert is_prime(1) is False


def test_is_prime_zero_is_not_prime():
    assert is_prime(0) is False
