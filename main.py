#! /usr/bin/env uv run

import process


def main() -> None:
    process.start_process(
        "python",
        ["--version"],
        raise_on_exitcode=True,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        print(e)
    finally:
        print("Press <Enter> to exit...")
        input()
