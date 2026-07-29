def test_apply_discount():
    order = make_order(total=100)
    coupon = make_coupon(percent_off=0.1)
    apply_discount(order, coupon)
