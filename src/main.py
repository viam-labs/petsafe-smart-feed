import asyncio

from viam.components.generic import Generic
from viam.module.module import Module

from .models.feeder import PetSafeFeeder


async def main() -> None:
    module = Module.from_args()
    module.add_model_from_registry(Generic.API, PetSafeFeeder.MODEL)
    await module.start()


if __name__ == "__main__":
    asyncio.run(main())
