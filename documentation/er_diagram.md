```mermaid
erDiagram
    PLAYER ||--o{ GAME : "plays as white"
    PLAYER ||--o{ GAME : "plays as black"
    GAME ||--o{ MOVE : "contains"

    PLAYER {
        int player_id
        string username
        string platform
    }
    GAME {
        int game_id
        int white_player_id
        int black_player_id
        string result
        date date_played
        string time_control
        int white_elo
        int black_elo
        string eco
        string opening_name
        int total_moves
    }
    MOVE {
        int move_id
        int game_id
        int ply
        int move_number
        string color
        string move_san
        float clock_seconds
        float time_spent_seconds
    }