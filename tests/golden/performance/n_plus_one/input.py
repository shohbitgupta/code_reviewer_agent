def get_order_totals(db_conn, order_ids):
    totals = []
    for order_id in order_ids:
        row = db_conn.execute("SELECT total FROM orders WHERE id = ?", (order_id,)).fetchone()
        totals.append(row[0])
    return totals
