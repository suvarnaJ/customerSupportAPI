import os
from dotenv import load_dotenv
from pymongo import AsyncMongoClient

load_dotenv()

async def main():
    client = AsyncMongoClient(
        os.environ["MONGODB_URL"],
        serverSelectionTimeoutMS=10000,
    )

    try:
        result = await client.admin.command("ping")
        print("MongoDB connection successful:", result)
    finally:
        await client.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())