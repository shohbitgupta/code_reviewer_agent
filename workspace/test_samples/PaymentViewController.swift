// PaymentViewController.swift
// Intentionally violates multiple coding standards for validation

import UIKit

// ── ARCH002: Presentation layer directly importing Data layer ─────────────────
// A ViewController should go through a ViewModel / UseCase, not hit Repository directly.
class PaymentViewController: UIViewController {

    // SEC001: Hardcoded API key and secret
    let apiKey = "sk_live_4xMn9QzW8rTpL2vK"
    let stripeSecret = "whsec_p3X7mNqR9tLw4Yz"
    let dbPassword = "SuperSecret@2024!"

    // GEN002: Magic numbers scattered through the class
    var retryCount = 0
    var maxAmount = 999999

    // PaymentRepository lives in the data layer — ViewController shouldn't own this
    let paymentRepository = PaymentRepository()
    let userRepository    = UserRepository()

    override func viewDidLoad() {
        super.viewDidLoad()
        loadPaymentHistory()
    }

    // GEN001: This function is 60+ lines — well over the 50-line limit
    func processPayment(amount: Double, cardNumber: String, cvv: String, expiry: String) {
        // GEN002: Magic numbers — 3, 500, 10000, 0.03 have no named constant
        if amount < 3 {
            showError("Amount too small")
            return
        }
        if amount > 10000 {
            showError("Amount exceeds limit")
            return
        }

        // SEC001: Printing sensitive card data to console
        print("Processing card: \(cardNumber), CVV: \(cvv)")

        // GEN002: Magic number 500 used as a threshold without explanation
        let fee = amount > 500 ? amount * 0.03 : 1.5

        // ARCH002: ViewController calling Repository directly (should go via ViewModel/UseCase)
        let user = userRepository.getCurrentUser()

        // Force-unwrap without nil check — crash risk
        let userId = user!.id
        let userName = user!.name

        // No error handling — bare try! that crashes on failure
        let result = try! paymentRepository.charge(
            userId: userId,
            amount: amount,
            fee: fee,
            card: cardNumber
        )

        if result.success {
            // GEN002: Magic number 200 — should be an HTTP status constant
            if result.statusCode == 200 {
                showSuccess("Payment of \(amount) processed for \(userName)")
            }
        } else {
            // Swallowing the error — no logging, no propagation
            retryCount += 1
            if retryCount < 3 {
                // Recursive retry without backoff
                processPayment(amount: amount, cardNumber: cardNumber, cvv: cvv, expiry: expiry)
            }
        }

        // GEN002: Magic sleep duration — 2 seconds with no explanation
        Thread.sleep(forTimeInterval: 2)

        // Updating UI on background thread — threading violation
        DispatchQueue.global().async {
            self.updateTransactionTable()
        }

        // Dead code after recursive path
        validateCardExpiry(expiry: expiry)
        applyLoyaltyDiscount(userId: userId, amount: amount)
        sendReceiptEmail(userId: userId, amount: amount)
        logTransaction(userId: userId, amount: amount, fee: fee)
    }

    func loadPaymentHistory() {
        // ARCH002: Presentation directly querying the data layer
        let history = paymentRepository.fetchAll()
        print("Loaded \(history.count) transactions")
    }

    private func showError(_ message: String) {
        // GEN002: Magic number 3 for animation duration
        UIView.animate(withDuration: 3) {
            self.view.backgroundColor = .red
        }
        print("ERROR: \(message)")
    }

    private func showSuccess(_ message: String) {
        print("SUCCESS: \(message)")
    }

    private func updateTransactionTable() {
        // Should be on main thread — will crash UIKit
        tableView.reloadData()
    }

    private func validateCardExpiry(expiry: String) { }
    private func applyLoyaltyDiscount(userId: String, amount: Double) { }
    private func sendReceiptEmail(userId: String, amount: Double) { }
    private func logTransaction(userId: String, amount: Double, fee: Double) { }

    // GEN002: Magic number 12 — months in a year, should be a constant
    func isCardExpired(month: Int, year: Int) -> Bool {
        return month < 1 || month > 12 || year < 2024
    }
}
