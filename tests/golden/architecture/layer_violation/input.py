class OrderHistoryView:
    def render(self, user_id, db_conn):
        cursor = db_conn.execute("SELECT * FROM orders WHERE user_id = ?", (user_id,))
        rows = cursor.fetchall()
        return "\n".join(f"Order #{r[0]}: {r[1]}" for r in rows)
