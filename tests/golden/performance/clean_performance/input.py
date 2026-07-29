def get_order_totals(db_conn, order_ids):
    placeholders = ",".join("?" for _ in order_ids)
    rows = db_conn.execute(
        f"SELECT total FROM orders WHERE id IN ({placeholders})", order_ids
    ).fetchall()
    return [row[0] for row in rows]
