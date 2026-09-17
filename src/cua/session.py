"""Session provider: gets the surface into an authenticated state before a run.

Login is a precondition, not part of the recorded capability. Credentials come
from the environment and are typed straight into the page; they never reach the
LLM, the trace, the artifact or the logs.
"""

import os

from cua.surface import LabelStrategy, RoleStrategy, Surface, SurfaceError, Target


class FormLogin:
    def __init__(self, route: str = "/login", user_label: str = "User ID", password_label: str = "Password",
                 submit: str = "Sign On", user_env: str = "TARGET_APP_USER", password_env: str = "TARGET_APP_PASSWORD"):
        self.route = route
        self.user_label = user_label
        self.password_label = password_label
        self.submit = submit
        self.user_env = user_env
        self.password_env = password_env

    def sign_in(self, surface: Surface) -> None:
        user, password = os.environ.get(self.user_env), os.environ.get(self.password_env)
        if not user or not password:
            raise SurfaceError(f"set {self.user_env} and {self.password_env} in .env")
        surface.goto(self.route)
        surface.fill(surface.resolve(Target(strategies=[LabelStrategy(text=self.user_label)]), 5).handle, user)
        surface.fill(surface.resolve(Target(strategies=[LabelStrategy(text=self.password_label)]), 5).handle, password)
        surface.click(surface.resolve(Target(strategies=[RoleStrategy(role="button", name=self.submit)]), 5).handle)
        if any(f.url.startswith(self.route) for f in surface.observe().frames):
            raise SurfaceError("sign-in failed: still on the login page")
