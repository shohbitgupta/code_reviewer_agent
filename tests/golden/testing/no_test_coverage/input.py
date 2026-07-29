def apply_discount(order, coupon):
    if coupon.expired:
        raise ValueError("expired coupon")
    if coupon.percent_off < 0 or coupon.percent_off > 1:
        raise ValueError("invalid discount")
    return order.total * (1 - coupon.percent_off)
