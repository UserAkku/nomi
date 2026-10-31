import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(prog="widget")
    parser.add_argument("url")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args(argv)
