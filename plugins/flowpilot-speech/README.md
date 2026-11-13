# flowpilot-speech

Text for speech for [FlowPilot](https://github.com/darkdarin/flowpilot): `speech.text` goes
between an agent and text-to-speech and spells in words what a Russian voice would misread:

- English words by pronunciation, from the CMU Pronouncing Dictionary (Queen → квин), unknown
  ones by letter rules;
- acronyms letter by letter (TV → ти-ви, МЧС → эм-че-эс);
- numbers, times, ordinals, units and currencies, the unit agreeing with the number (5 °C → пять
  градусов, $3 → три доллара, 2-го → второго);
- built-in and your own replacements (Wi-Fi → вай-фай);
- optionally stress marks (`+` before the vowel), as Silero voices read them.

Streams whole words as they come and keeps the header, the text's language included; text in
another language than Russian goes on unchanged. No models: a few MB of dictionaries
(`cmudict`, `pymorphy3`) and `num2words`.
