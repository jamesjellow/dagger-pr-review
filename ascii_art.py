import random

ARTS = [
    r"""
     /\_/\
    ( o.o )
     > ^ <
    """,
    r"""
       __
     _(  )
    (____)
    """,
    r"""
     (\\\\)
     ( -_-)
     /|  |\
    """,
]


def get_random_art() -> str:
    """Return a randomly selected ASCII art string."""
    return random.choice(ARTS)


def print_random_art() -> None:
    """Print a random ASCII art to the console."""
    print(get_random_art())


if __name__ == "__main__":
    print_random_art()
