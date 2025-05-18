# FineCode Python Extension Runner

## Development Notes

- extension server could use tcp communication with workspace manager, but for some reason it didn't work in first tries: it blocked the server, client connection was accepted, but no requests were processed.

## Data Provider

dataKind: PythonPackageList

provider: PythonPackageListProvider

all data must be versioned

providers calculate data either automatically or on demand
