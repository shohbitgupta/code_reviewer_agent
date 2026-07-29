def find_user_by_email(db_conn, email):
    if not isinstance(email, str) or "@" not in email:
        raise ValueError("invalid email")
    cursor = db_conn.execute("SELECT * FROM users WHERE email = ?", (email,))
    return cursor.fetchone()
