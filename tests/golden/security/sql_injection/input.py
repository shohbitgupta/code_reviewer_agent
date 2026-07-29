def find_user_by_email(db_conn, email):
    query = "SELECT * FROM users WHERE email = '" + email + "'"
    cursor = db_conn.execute(query)
    return cursor.fetchone()
