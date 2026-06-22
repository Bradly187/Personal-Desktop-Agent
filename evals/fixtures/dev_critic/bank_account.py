"""Simple bank account — fixture for dev_critic eval (dc-02)."""


class BankAccount:
    def __init__(self, balance=0.0):
        self.balance = float(balance)

    def deposit(self, amount):
        if amount <= 0:
            raise ValueError("deposit amount must be positive")
        self.balance += amount
        return self.balance

    def get_balance(self):
        return self.balance
