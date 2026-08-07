"""Seed data for the date_activities catalog.

Provides ~20 diverse long-distance date activity entries across multiple
categories: co-watch, cook-along, game, virtual tour, creative, reading,
learning, and specialty. Each entry includes name, description, estimated
duration, tags, and source='seed'.

These are embedded on first insertion so the catalog is immediately
searchable via semantic (RAG) lookup.
"""

from app.schemas.catalog import DateActivityCreate

# ── Co-watch ─────────────────────────────────────────────────────────────

SEED_ACTIVITIES: list[DateActivityCreate] = [
    DateActivityCreate(
        name="Movie Night — Synchronised Streaming",
        description=(
            "Pick a movie on a streaming platform you both have access to, "
            "sync the start time with a countdown, and video-call during the "
            "credits to discuss. Use Teleparty or a similar browser extension "
            "for built-in sync and chat."
        ),
        est_duration_min=150,
        tags=["co-watch", "movie", "streaming", "low-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="TV Series Binge — Two Episodes",
        description=(
            "Start a new series together — watch two episodes back-to-back "
            "while on a video call, pausing between episodes to discuss "
            "theories and favourite moments."
        ),
        est_duration_min=90,
        tags=["co-watch", "tv-series", "streaming", "low-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Documentary & Discussion",
        description=(
            "Watch a documentary (nature, science, or history) together and "
            "spend 20 minutes afterwards discussing what you learned. "
            "Great for couples who enjoy learning together."
        ),
        est_duration_min=90,
        tags=["co-watch", "documentary", "educational", "low-energy"],
        source="seed",
    ),
    # ── Cook-along ────────────────────────────────────────────────────────
    DateActivityCreate(
        name="Cook Together — Same Recipe",
        description=(
            "Choose a recipe in advance, shop for ingredients separately, "
            "then cook together over a video call. Compare plating at the "
            "end and eat your 'same but different' meals together."
        ),
        est_duration_min=120,
        tags=["cook-along", "cooking", "interactive", "medium-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Virtual Coffee Date",
        description=(
            "Brew your favourite coffee or tea and sit down for a relaxed "
            "video call. No agenda — just catch up like you would at a cafè."
        ),
        est_duration_min=45,
        tags=["cook-along", "coffee", "casual", "low-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Bake-Off Challenge",
        description=(
            "Pick the same dessert recipe, bake it simultaneously on video "
            "call, then vote on whose turned out best. Loser buys the next "
            "streaming rental."
        ),
        est_duration_min=120,
        tags=["cook-along", "baking", "challenge", "fun", "medium-energy"],
        source="seed",
    ),
    # ── Game ──────────────────────────────────────────────────────────────
    DateActivityCreate(
        name="Online Board Game Night",
        description=(
            "Play a digital board or card game together — options include "
            "Catan Universe, Uno, Codenames, or Tabletopia. Voice chat "
            "during the game for banter and strategy."
        ),
        est_duration_min=60,
        tags=["game", "board-game", "competitive", "medium-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Co-op Video Game Session",
        description=(
            "Play a cooperative video game together — titles like 'It Takes "
            "Two', 'Stardew Valley', or 'Portal 2' work well for long-distance "
            "couples. Screen-share or use in-game voice chat."
        ),
        est_duration_min=90,
        tags=["game", "co-op", "video-game", "medium-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Trivia Quiz Night",
        description=(
            "Take turns hosting a trivia quiz on a theme of your choice. "
            "Use a shared screen or a quiz app. Keep score across multiple "
            "sessions for a running competition."
        ),
        est_duration_min=60,
        tags=["game", "trivia", "quiz", "competitive", "low-energy"],
        source="seed",
    ),
    # ── Virtual Tour ──────────────────────────────────────────────────────
    DateActivityCreate(
        name="Virtual Museum Tour",
        description=(
            "Explore a world-famous museum together via its online virtual "
            "tour — the Louvre, the British Museum, or the Smithsonian. "
            "Screen-share and take turns picking galleries."
        ),
        est_duration_min=60,
        tags=["virtual-tour", "museum", "culture", "educational", "low-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Google Earth Adventure",
        description=(
            "Take turns dropping a pin on Google Earth somewhere neither of "
            "you has been. Explore the street view, read about the location, "
            "and add it to your 'future visit' list."
        ),
        est_duration_min=45,
        tags=["virtual-tour", "travel", "exploration", "low-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Stargazing Together",
        description=(
            "Use a stargazing app (like SkyView or Stellarium) while on a "
            "video call. Point your phones at the sky, share screens, and "
            "identify constellations, planets, and satellites."
        ),
        est_duration_min=45,
        tags=["virtual-tour", "nature", "stargazing", "romantic", "low-energy"],
        source="seed",
    ),
    # ── Creative ──────────────────────────────────────────────────────────
    DateActivityCreate(
        name="Draw Together — Same Prompt",
        description=(
            "Agree on a drawing prompt, set a 20-minute timer, and draw "
            "individually. Share the results and vote on whose is better. "
            "No artistic skill required — the fun is in the contrast."
        ),
        est_duration_min=45,
        tags=["creative", "drawing", "art", "fun", "low-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Collaborative Playlist",
        description=(
            "Create a shared Spotify/Apple Music playlist. Each person adds "
            "5 songs that remind them of the other, then listen through "
            "together, explaining why each song was chosen."
        ),
        est_duration_min=60,
        tags=["creative", "music", "playlist", "romantic", "low-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Write a Short Story Together",
        description=(
            "Start a shared Google Doc. One person writes the first "
            "paragraph, then passes it to the other. Alternate until you "
            "have a short story. Read it aloud at the end."
        ),
        est_duration_min=60,
        tags=["creative", "writing", "storytelling", "collaborative", "low-energy"],
        source="seed",
    ),
    # ── Reading ───────────────────────────────────────────────────────────
    DateActivityCreate(
        name="Couple's Book Club",
        description=(
            "Pick a book to read in parallel over a week. Meet on video call "
            "to discuss the assigned chapters — share favourite quotes, "
            "predict what happens next, and rate the book together."
        ),
        est_duration_min=60,
        tags=["reading", "book-club", "intellectual", "low-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Poetry Reading",
        description=(
            "Take turns reading poems aloud to each other. Each person picks "
            "2-3 poems beforehand — they can be classic, contemporary, or "
            "silly. Discuss what each poem means to you."
        ),
        est_duration_min=45,
        tags=["reading", "poetry", "romantic", "low-energy"],
        source="seed",
    ),
    # ── Learning ──────────────────────────────────────────────────────────
    DateActivityCreate(
        name="Language Practice Together",
        description=(
            "Spend 30 minutes practising a language you're both learning. "
            "Use a shared app (Duolingo, Babbel) or practise conversation "
            "with simple phrases in the target language."
        ),
        est_duration_min=45,
        tags=["learning", "language", "educational", "medium-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Online Course — Watch Together",
        description=(
            "Enrol in a free online course (Coursera, edX, or Khan Academy) "
            "on a topic you're both curious about. Watch one lecture per "
            "session and discuss the key takeaways."
        ),
        est_duration_min=60,
        tags=["learning", "course", "educational", "intellectual", "low-energy"],
        source="seed",
    ),
    # ── Specialty ─────────────────────────────────────────────────────────
    DateActivityCreate(
        name="Virtual Wine or Tea Tasting",
        description=(
            "Choose the same bottle of wine (or type of tea) in advance. "
            "Taste together on video call, describing the flavours, aromas, "
            "and rating each sip. A sommelier kit (same snacks, same "
            "glassware) adds to the experience."
        ),
        est_duration_min=60,
        tags=["specialty", "tasting", "wine", "tea", "romantic", "medium-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Workout Together",
        description=(
            "Follow the same YouTube workout video simultaneously on a video "
            "call. Pick a session length that suits both fitness levels — "
            "yoga, HIIT, or dance cardio."
        ),
        est_duration_min=45,
        tags=["specialty", "fitness", "workout", "active", "high-energy"],
        source="seed",
    ),
    DateActivityCreate(
        name="Meditation Session",
        description=(
            "Use a guided meditation app (Headspace, Calm) together. "
            "Start the same session at the same time, stay on the call in "
            "silence, and spend 5 minutes afterwards sharing how you feel."
        ),
        est_duration_min=30,
        tags=["specialty", "meditation", "wellness", "relaxation", "low-energy"],
        source="seed",
    ),
]