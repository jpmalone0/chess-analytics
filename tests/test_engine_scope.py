"""What a search decides to evaluate.

Evaluation is opt-in because the corpus is 17M plies. Scope is what keeps a run
bounded, so a filter that silently widens costs real minutes, and one that
silently narrows leaves a player looking at a half-analyzed window without being
told.
"""

from datetime import date

import pytest
from sqlalchemy import text

from engine.scope import Scope, UnknownPlayer, resolve_scope, unanalyzed
from tests.conftest import make_game, make_player


@pytest.fixture
def corpus(db):
    """One player with games across two time classes, dates, and a variant."""
    me = make_player(db, "me")
    them = make_player(db, "them")

    make_game(db, me, them, 1500, 1500, time_class="bullet",
              date_played=date(2026, 1, 10), end_time=None)
    make_game(db, me, them, 1500, 1500, time_class="bullet",
              date_played=date(2026, 3, 20), end_time=None)
    make_game(db, me, them, 1500, 1500, time_class="rapid",
              date_played=date(2026, 3, 20), end_time=None)
    make_game(db, them, me, 1500, 1500, time_class="bullet",
              date_played=date(2026, 3, 21), end_time=None)
    make_game(db, me, them, 1500, 1500, time_class="bullet",
              date_played=date(2026, 3, 22), end_time=None, variant="chess960")
    db.commit()
    return me


class TestFilters:
    def test_unknown_player_is_an_error_not_an_empty_scope(self, db, corpus):
        """Returning zero games for a typo'd username would read as 'nothing to
        analyze' and look like success."""
        with pytest.raises(UnknownPlayer):
            resolve_scope(db, Scope(username="nobody"))

    def test_time_class_narrows_the_scope(self, db, corpus):
        both = resolve_scope(db, Scope(username="me"))
        bullet = resolve_scope(db, Scope(username="me", time_class="bullet"))
        assert len(both) == 4      # 5 games less the variant
        assert len(bullet) == 3

    def test_variant_games_are_never_analyzed(self, db, corpus):
        """They are excluded everywhere else in the app; spending engine time on
        a Chess960 game would also put it in reach of aggregates that assume
        standard chess."""
        all_games = resolve_scope(db, Scope(username="me"))
        variants = db.execute(
            text("SELECT game_id FROM games WHERE variant IS NOT NULL")
        ).scalars().all()
        assert variants and not set(variants) & set(all_games)

    def test_colour_narrows_the_scope(self, db, corpus):
        as_black = resolve_scope(db, Scope(username="me", player_color="black"))
        assert len(as_black) == 1

    def test_limit_caps_the_scope(self, db, corpus):
        assert len(resolve_scope(db, Scope(username="me", limit=2))) == 2


class TestDateWindow:
    def test_both_bounds_are_inclusive(self, db, corpus):
        """An exclusive end date silently drops the most recent day, which is
        the one a player is most likely to be looking at."""
        got = resolve_scope(db, Scope(
            username="me", time_class="bullet",
            start_date=date(2026, 3, 20), end_date=date(2026, 3, 21),
        ))
        assert len(got) == 2

    def test_an_open_start_reaches_back_to_the_beginning(self, db, corpus):
        got = resolve_scope(db, Scope(
            username="me", time_class="bullet", end_date=date(2026, 1, 10),
        ))
        assert len(got) == 1

    def test_a_window_with_no_games_is_empty_not_an_error(self, db, corpus):
        got = resolve_scope(db, Scope(
            username="me", start_date=date(2020, 1, 1), end_date=date(2020, 12, 31),
        ))
        assert got == []


class TestUnplayableGames:
    def test_a_game_with_no_moves_is_excluded(self, db):
        """Nothing to replay. Included, it would write a 'failed' coverage row
        on every run and never stop being retried."""
        me = make_player(db, "me")
        them = make_player(db, "them")
        make_game(db, me, them, 1500, 1500,
                  white_move_times=[], black_move_times=[])
        db.commit()
        assert resolve_scope(db, Scope(username="me")) == []

    def test_a_one_ply_game_is_excluded(self, db):
        """Two positions bound one move, but a single ply scores nothing an
        aggregate can use."""
        me = make_player(db, "me")
        them = make_player(db, "them")
        make_game(db, me, them, 1500, 1500,
                  white_move_times=[5.0], black_move_times=[])
        db.commit()
        assert resolve_scope(db, Scope(username="me")) == []


class TestResume:
    @pytest.fixture
    def coverage(self, db):
        """Attach an empty engine database so coverage can be seeded."""
        db.execute(text("ATTACH DATABASE ':memory:' AS engine"))
        db.execute(text("""
            CREATE TABLE engine.game_coverage (
                run_id INTEGER NOT NULL, game_id INTEGER NOT NULL,
                plies_analyzed INTEGER NOT NULL, status VARCHAR(20) NOT NULL,
                error TEXT, completed_at TIMESTAMP,
                PRIMARY KEY (run_id, game_id)
            )
        """))
        return db

    def mark(self, db, game_id, status, run_id=1):
        db.execute(
            text("INSERT INTO engine.game_coverage "
                 "(run_id, game_id, plies_analyzed, status) "
                 "VALUES (:r, :g, 0, :s)"),
            {"r": run_id, "g": game_id, "s": status},
        )

    def test_completed_games_are_skipped(self, coverage, corpus):
        """Widening a date window by a day should cost a day of evaluation, not
        the whole window again."""
        ids = resolve_scope(coverage, Scope(username="me"))
        self.mark(coverage, ids[0], "complete")
        assert unanalyzed(coverage, ids, run_id=1) == ids[1:]

    def test_failed_and_partial_games_are_retried(self, coverage, corpus):
        """A game that died to a killed batch or a transient engine error is
        unfinished work, not a decision."""
        ids = resolve_scope(coverage, Scope(username="me"))
        self.mark(coverage, ids[0], "failed")
        self.mark(coverage, ids[1], "partial")
        assert unanalyzed(coverage, ids, run_id=1) == ids

    def test_another_run_does_not_count_as_coverage(self, coverage, corpus):
        """Depth 20 results say nothing about what depth 14 has covered."""
        ids = resolve_scope(coverage, Scope(username="me"))
        self.mark(coverage, ids[0], "complete", run_id=2)
        assert unanalyzed(coverage, ids, run_id=1) == ids

    def test_an_empty_scope_needs_no_query(self, coverage):
        assert unanalyzed(coverage, [], run_id=1) == []
