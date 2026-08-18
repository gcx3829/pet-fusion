export function getCenteredOffsets(count: number, nodeHeight: number, nodeGapY: number): number[] {
  const stride = nodeHeight + nodeGapY;
  return Array.from({ length: count }, (_, index) => (
    index - (count - 1) / 2
  ) * stride);
}

