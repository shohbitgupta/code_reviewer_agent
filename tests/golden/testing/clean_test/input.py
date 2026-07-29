def test_apply_discount_reduces_total_by_percent_off():
    order = make_order(total=100)
    coupon = make_coupon(percent_off=0.1)

    result = apply_discount(order, coupon)

    assert result == 90
