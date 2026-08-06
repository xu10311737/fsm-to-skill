def main(row, threshold):
    total = sum(row)
    return {'row-sum': total, 'decision': 'big' if total > threshold else 'small'}
