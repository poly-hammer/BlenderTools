"""Validate the exact native result of a requested Hair Guide Cloth build."""
import json

PREFIX = 'SEND2UE_HAIR_GUIDE_CLOTH:'


def validate_import_response(response, record):
    receipts = []
    for line in str(response or '').splitlines():
        if PREFIX in line:
            try:
                receipt, _ = json.JSONDecoder().raw_decode(line.split(PREFIX, 1)[1].lstrip())
            except (ValueError, TypeError) as error:
                raise RuntimeError('Hair Guide Cloth returned an unreadable build receipt') from error
            receipts.append(receipt)
    if len(receipts) != 1 or not isinstance(receipts[0], dict):
        raise RuntimeError('Hair Guide Cloth did not return one verified build receipt')
    receipt = receipts[0]
    if receipt.get('verified') is not True:
        raise RuntimeError('Hair Guide Cloth was not updated: ' + str(receipt.get('detail') or receipt.get('status') or 'unverified result'))
    expected = record.get('simulation_enabled', True)
    if (receipt.get('render_asset_path') != record['render_asset_path'] or
            receipt.get('manifest_sha256') != record['manifest_sha256'] or
            receipt.get('simulation_enabled') is not expected):
        raise RuntimeError('Hair Guide Cloth receipt does not match this exported unit')
    if expected and (receipt.get('sim_asset_path') != record['sim_asset_path'] or
                     not receipt.get('cloth_asset_path') or not receipt.get('binding_asset_path')):
        raise RuntimeError('Hair Guide Cloth receipt has no verified simulation output')
    return receipt
