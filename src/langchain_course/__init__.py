from dotenv import load_dotenv

load_dotenv()
import os


def main() -> None:
    print("Hello from langchain-course!")
    print(os.getenv("OPENAI_API_KEY"))


if __name__ == "__main__":
    main()
