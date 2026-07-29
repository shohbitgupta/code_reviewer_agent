class UserAccountManager:
    def fetch_from_db(self, user_id):
        return self.db.query(user_id)

    def send_welcome_email(self, user):
        self.mailer.send(user.email, "Welcome!")

    def export_to_csv(self, users):
        return "\n".join(f"{u.id},{u.email}" for u in users)

    def render_profile_html(self, user):
        return f"<div>{user.name}</div>"
