import pytest

@pytest.mark.asyncio
async def test_catalog_seed(client):
    response = await client.get("/catalog/seed")
    assert response.status_code == 200

@pytest.mark.asyncio
async def test_catalog_search(client):
    res = await client.post(
        "/catalog/search",
        json={"query_text":"virtual cook-along", "max_results":3}
    )
    assert res.status_code == 200

@pytest.mark.asyncio
async def test_catalog_get(client):
    """Testing the catalog get router is still reachable before others even when in future
    edits we may add a new router by {activity_id}"""

    #getting source data by seed
    seed_res = await client.get("/catalog?source=seed&limit=1")
    seeded = seed_res.json()
    if not seeded:
        pytest.skip("No seeded data surfaced to fetch by id")

    activity_id = seeded[0]["id"]
    res = await client.get(f"/catalog/{activity_id}")
    assert res.status_code == 200
    assert res.json()["id"] == activity_id