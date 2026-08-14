import asyncio
from app import db
from sqlalchemy import text

async def main():
	async with db.session() as session:
		result = await session.execute(text("SELECT 1"))
		print("connected:", result.scalar())

asyncio.run(main())


