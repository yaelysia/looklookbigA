import test_upcoming_events_completion_base as base
import test_upcoming_events_source_failures as source_failures


def main():
    base.main()
    source_failures.main()


if __name__ == "__main__":
    main()
