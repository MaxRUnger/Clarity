(function () {
  function normalizeSpaces(text) {
    return String(text == null ? '' : text).trim().split(/\s+/).filter(Boolean).join(' ');
  }

  function nameKey(name) {
    const text = normalizeSpaces(name).toLowerCase();
    if (text.indexOf(',') === -1) return text;
    return text.split(',').map(normalizeSpaces).join(', ');
  }

  function displayName(fullName) {
    const raw = fullName == null ? '' : String(fullName);
    if (!raw.trim()) return 'Unnamed student';
    return raw;
  }

  const collator = new Intl.Collator('en-US');

  function compareNames(a, b) {
    const left = a == null ? '' : String(a);
    const right = b == null ? '' : String(b);
    const leftBlank = !left.trim();
    const rightBlank = !right.trim();
    if (leftBlank && rightBlank) return 0;
    if (leftBlank) return 1;
    if (rightBlank) return -1;
    return collator.compare(left, right);
  }

  const commaRequiredMessage =
    'Enter the name as Last, First, with a comma. Example: Velasco Jr, Emilio.';

  window.StudentNames = {
    normalizeSpaces,
    nameKey,
    displayName,
    compareNames,
    commaRequiredMessage,
  };
})();
