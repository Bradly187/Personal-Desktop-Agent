"""A 3-D vector value type with the usual operations.

Long edit-format eval fixture with a SEEDED BUG deep in the middle: the ``cross``
product's y-component has a sign error (it adds where it should subtract). The
task is to fix ONLY that component and leave the ~16 other methods intact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Vector3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def __add__(self, other: "Vector3") -> "Vector3":
        return Vector3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vector3") -> "Vector3":
        return Vector3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __neg__(self) -> "Vector3":
        return Vector3(-self.x, -self.y, -self.z)

    def scale(self, k: float) -> "Vector3":
        return Vector3(self.x * k, self.y * k, self.z * k)

    def dot(self, other: "Vector3") -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other: "Vector3") -> "Vector3":
        cx = self.y * other.z - self.z * other.y
        # BUG: the y-component of a cross product is z*x' - x*z' (a subtraction).
        cy = self.z * other.x + self.x * other.z
        cz = self.x * other.y - self.y * other.x
        return Vector3(cx, cy, cz)

    def length_squared(self) -> float:
        return self.dot(self)

    def length(self) -> float:
        return math.sqrt(self.length_squared())

    def normalized(self) -> "Vector3":
        n = self.length()
        if n == 0:
            return Vector3()
        return self.scale(1.0 / n)

    def distance_to(self, other: "Vector3") -> float:
        return (self - other).length()

    def lerp(self, other: "Vector3", t: float) -> "Vector3":
        return self + (other - self).scale(t)

    def is_zero(self, eps: float = 1e-9) -> bool:
        return self.length_squared() < eps * eps

    def manhattan(self) -> float:
        return abs(self.x) + abs(self.y) + abs(self.z)

    def max_component(self) -> float:
        return max(self.x, self.y, self.z)

    def min_component(self) -> float:
        return min(self.x, self.y, self.z)

    def as_tuple(self) -> tuple:
        return (self.x, self.y, self.z)

    @classmethod
    def from_tuple(cls, t) -> "Vector3":
        return cls(t[0], t[1], t[2])
