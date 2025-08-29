# dyne/musl-action

This action sets up dyne-gcc-musl toolchain for cross-compilation to your target architecture, caching it for performance.

## 🎮 Usage

```yaml
- uses: dyne/musl-action@main
  with:
    # Target architecture
    # Can be x86_64, arm_64, arm_hf, riscv_64
    # Default: x86_64
    target_arch: ''
```

## 🎣 Outputs

The following outputs can be accessed via `${{ steps.<step-id>.outputs }}` from this action

| Name | Type | Description |
| ---- | ---- | ----------- |
| `CC` | String | Path to musl-gcc |
| `CXX` | String | Path to musl-g++ |
