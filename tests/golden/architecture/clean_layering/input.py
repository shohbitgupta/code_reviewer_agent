class OrderHistoryView:
    def __init__(self, order_service):
        self.order_service = order_service

    def render(self, user_id):
        orders = self.order_service.get_orders_for_user(user_id)
        return "\n".join(f"Order #{o.id}: {o.total}" for o in orders)
