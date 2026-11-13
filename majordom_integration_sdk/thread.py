"""Thread operational datasets, as hex TLVs: what the Hub, border routers and integrations read of them."""

CHANNEL, EXTENDED_PAN_ID = 0, 2  # TLV types


def dataset_tlvs(dataset: str) -> dict[int, bytes]:
    """A dataset's TLVs by type: the Extended PAN ID tells networks apart, whatever moved since (the channel).
    ValueError for one that isn't hex."""
    data, tlvs = bytes.fromhex(dataset), {}
    while len(data) >= 2:
        tlvs[data[0]], data = data[2 : 2 + data[1]], data[2 + data[1] :]
    return tlvs


def dataset_channel(dataset: str) -> int:
    """The channel of the network of ``dataset``; KeyError for one without."""
    return int.from_bytes(dataset_tlvs(dataset)[CHANNEL][1:])  # after the channel page
