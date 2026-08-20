import asyncio
import sqlite3

from shinjuku_service import ShinjukuService


def test_mahjong_rank_is_optional_and_readable(tmp_path):
    path = tmp_path / "shinjuku.db"

    async def prepare():
        service = ShinjukuService(str(path))
        registered = await service.register("12345")
        assert await service.mahjong_rank("QQ:12345") is None
        user_id = registered["user"]["id"]
        await service.close()
        return user_id

    user_id = asyncio.run(prepare())
    conn = sqlite3.connect(path)
    conn.execute(
        '''CREATE TABLE "MahjongRank" (
            "platformId" TEXT PRIMARY KEY, "userId" INTEGER, "rating" REAL,
            "rankPoints" INTEGER, "games" INTEGER, "firstCount" INTEGER,
            "secondCount" INTEGER, "thirdCount" INTEGER, "fourthCount" INTEGER
        )'''
    )
    conn.execute(
        'INSERT INTO "MahjongRank" VALUES (?,?,?,?,?,?,?,?,?)',
        ("12345", user_id, 1516.0, 150, 1, 1, 0, 0, 0),
    )
    conn.commit()
    conn.close()

    async def read_rank():
        service = ShinjukuService(str(path))
        rank = await service.mahjong_rank("QQ:12345")
        await service.close()
        return rank

    rank = asyncio.run(read_rank())
    assert rank is not None
    assert rank["rankPoints"] == 150
    assert rank["rating"] == 1516.0

