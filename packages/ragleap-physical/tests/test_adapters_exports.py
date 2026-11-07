"""Every adapter name a user needs can be imported from ragleap_physical.adapters."""
import ragleap_physical.adapters as adapters
from ragleap_physical.adapters import Item
from ragleap_physical.adapters.mtconnect import Item as MTConnectItem


def test_item_is_exported_and_is_the_mtconnect_item():
    assert Item is MTConnectItem
    assert "Item" in adapters.__all__


def test_every_name_in_all_can_be_imported():
    for name in adapters.__all__:
        assert hasattr(adapters, name), name
