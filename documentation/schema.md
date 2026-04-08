# Entities
### Players
Any unique user (username) on chess.com (platform) with a unique user id (player_id)

### Games
Any single game (game_id) played between two players (white_player_id, black_player_id) with other properties like result, date_played, time_control, opening_name, etc.

### Moves
All the moves contained within a specific game (move_id) with other properties like ply, move_number, color, move_san, clock_seconds, and time_spent_seconds


# Relationships & Cardinalities

### Players to Games
A player participates in a game as either white or black, identified by player_id. One player can have many games, so the relationship is 1:N. 

### Games to Moves
A game contains a list of moves. One game can have many moves, so the relationship is 1:N.