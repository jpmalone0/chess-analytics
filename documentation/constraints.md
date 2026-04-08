### Player Table Constraints
Player_id is a primary key so that every player record can be uniquely identified. This implies uniqueness and not null.
Username must be unique and not null to prevent the database from duplicating records of the same player and to ensure that every player has a username.
Platform must be not null to ensure we know where the game was played.
### Game Table Constraints
Game_id is a primary key that uniquely identifies each recorded game. This implies uniqueness and not null.
white_player_id and black_player_id must be foreign keys that reference the player_id in the player table because games should only exist in the database if they were played by known players. They also must be not null.
Result must be not null, as every game has an outcome and statstics would break if this was not available.
White_elo, black_elo, and total_moves should all be integers to properly perform calculations, while time_control, eco, and opening_name should all be strings to allow for easy searching. Date_played should, of course, be a date to ensure time window functionality works as intended.


### Move Table Constraints
