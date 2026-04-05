-- ================================================================
-- Chess Analytics Database Schema
-- ================================================================

-- Players table — one row per unique chess.com username
CREATE TABLE IF NOT EXISTS players (
    player_id   SERIAL PRIMARY KEY,
    username    VARCHAR(100) NOT NULL UNIQUE,
    platform    VARCHAR(20) NOT NULL DEFAULT 'chess.com'
);

-- Games table — one row per completed game
CREATE TABLE IF NOT EXISTS games (
    game_id             SERIAL PRIMARY KEY,
    white_player_id     INT NOT NULL REFERENCES players(player_id),
    black_player_id     INT NOT NULL REFERENCES players(player_id),
    result              VARCHAR(10) NOT NULL,        -- '1-0', '0-1', '1/2-1/2'
    date_played         DATE,
    time_control        VARCHAR(30),                 -- e.g. '180', '600+2'
    time_class          VARCHAR(20),                 -- bullet, blitz, rapid, daily
    white_elo           INT,
    black_elo           INT,
    white_accuracy      FLOAT,                       -- from chess.com API (nullable)
    black_accuracy      FLOAT,                       -- from chess.com API (nullable)
    eco                 VARCHAR(10),                 -- e.g. 'B34'
    opening_name        VARCHAR(255),
    termination         VARCHAR(255),                -- e.g. 'ballasack6 won by resignation'
    chess_com_url       VARCHAR(255) UNIQUE,          -- dedup key
    total_moves         INT,
    created_at          TIMESTAMP DEFAULT NOW()
);

-- Moves table — one row per half-move (ply)
CREATE TABLE IF NOT EXISTS moves (
    move_id                 SERIAL PRIMARY KEY,
    game_id                 INT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    ply                     INT NOT NULL,              -- 1-indexed half-move number
    move_number             INT NOT NULL,              -- full move number (1, 2, 3...)
    color                   VARCHAR(5) NOT NULL,       -- 'white' or 'black'
    move_san                VARCHAR(10) NOT NULL,      -- e.g. 'Nf3', 'O-O', 'exd5'
    clock_seconds           FLOAT,                     -- remaining clock after this move
    time_spent_seconds      FLOAT,                     -- time consumed on this move
    UNIQUE (game_id, ply)
);




-- ================================================================
-- Indexes for analytical query performance
-- ================================================================

CREATE INDEX IF NOT EXISTS idx_games_white_player  ON games(white_player_id);
CREATE INDEX IF NOT EXISTS idx_games_black_player  ON games(black_player_id);
CREATE INDEX IF NOT EXISTS idx_games_date          ON games(date_played);
CREATE INDEX IF NOT EXISTS idx_games_time_class    ON games(time_class);
CREATE INDEX IF NOT EXISTS idx_moves_game_ply      ON moves(game_id, ply);
CREATE INDEX IF NOT EXISTS idx_moves_game_color    ON moves(game_id, color);
