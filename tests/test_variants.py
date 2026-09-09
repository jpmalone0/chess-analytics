"""Variant games must not reach the analytics.

chess.com's archive mixes Chess960, bughouse and odds games in with standard
ones and labels them with the same time_class, but their Elo comes from a
separate pool. Left in, a 2300 Chess960 rating and a 3100 blitz rating end up
on one line — the rating chart grows spikes that look like corrupt data.
"""

from datetime import date

from app import crud
from etl.parse_pgn import _extract_variant
from tests.conftest import make_game, make_player

STANDARD_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class TestExtractVariant:
    def test_standard_game_has_no_variant(self):
        """A normal chess.com PGN carries neither Variant nor SetUp."""
        assert _extract_variant({"Event": "Live Chess", "ECO": "B23"}) is None

    def test_chess960_header(self):
        headers = {
            "Event": "Live Chess - Chess960",
            "Variant": "Chess960",
            "SetUp": "1",
            "FEN": "nrqbbknr/pppppppp/8/8/8/8/PPPPPPPP/NRQBBKNR w HBhb - 0 1",
        }
        assert _extract_variant(headers) == "chess960"

    def test_variant_name_is_slugified(self):
        assert _extract_variant({"Variant": "King of the Hill"}) == "kingofthehill"
        assert _extract_variant({"Variant": "Three-check"}) == "threecheck"

    def test_setup_without_variant_name_is_still_non_standard(self):
        """Backstop: a position set up without naming a variant is not standard."""
        headers = {"SetUp": "1", "FEN": "8/8/8/8/8/8/8/K6k w - - 0 1"}
        assert _extract_variant(headers) == "unknown"

    def test_setup_at_the_standard_position_is_standard(self):
        headers = {"SetUp": "1", "FEN": STANDARD_FEN}
        assert _extract_variant(headers) is None

    def test_blank_variant_header_is_standard(self):
        assert _extract_variant({"Variant": "   "}) is None


class TestVariantGamesAreExcluded:
    """The regression guard: a variant game carries an Elo from another pool,
    so every query that reports rating or record must skip it."""

    def _seed(self, db):
        player = make_player(db, "player")
        rival = make_player(db, "rival")
        for i in range(5):
            make_game(db, player, rival, white_elo=3100, black_elo=3090,
                      time_class="blitz", time_control="180", end_time=1700000000 + i,
                      date_played=date(2024, 5, 1))
        # Same player, same time_class, ~700 points lower — the spike.
        # A loss, unlike the five standard wins, so any query that counts it
        # moves a rate off 100% and gives itself away.
        make_game(db, player, rival, white_elo=2350, black_elo=2340,
                  time_class="blitz", time_control="180", end_time=1700000100,
                  date_played=date(2024, 5, 2), result="0-1", variant="chess960")
        db.commit()
        return player

    def test_elo_history_omits_variant_games(self, db):
        player = self._seed(db)
        points = crud.elo_history(db, player.player_id, time_class="blitz")
        elos = [p["elo"] for p in points]
        assert len(elos) == 5
        assert min(elos) == 3100, "the Chess960 rating leaked onto the standard line"

    def test_stats_omit_variant_games(self, db):
        player = self._seed(db)
        stats = crud.get_player_stats(db, player.player_id, time_class="blitz")
        assert stats["total_games"] == 5

    def test_top_openings_omit_variant_games(self, db):
        player = self._seed(db)
        openings = crud.get_top_openings(db, player.player_id, time_class="blitz")
        assert openings["totals"]["white"]["games"] == 5

    def test_winrate_by_color_omits_variant_games(self, db):
        player = self._seed(db)
        rows = crud.winrate_by_color_rolling(db, player.player_id, time_class="blitz")
        # Every game seeded here is a white win, so a clean series is 100%.
        assert all(r["white"] == 100.0 for r in rows)

    def test_null_variant_is_treated_as_standard(self, db):
        """Rows loaded before the column existed are NULL and must keep counting
        — the backfill is opt-in, so unknown history cannot start disappearing."""
        player = self._seed(db)
        assert crud.get_player_stats(db, player.player_id)["total_games"] == 5
