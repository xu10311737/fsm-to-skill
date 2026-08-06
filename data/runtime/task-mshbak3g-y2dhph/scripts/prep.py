def main(numbers, threshold):
    groups = []
    for i in range(0, len(numbers), 2):
        groups.append(numbers[i:i+2])
    return {'groups': groups}
