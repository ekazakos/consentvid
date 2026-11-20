// Display labels for the consent vocabulary, shared by the page and the summary.

export const LABELS = {
  modalities: {
    video: 'The picture',
    audio: 'The sound',
    transcript: 'A transcript of the speech',
    face: 'Identifiable faces (e.g. face recognition)',
    voice: 'Identifiable voices (e.g. voice cloning)',
  },
  purposes: { training: 'Training AI models', evaluation: 'Evaluating models', benchmarking: 'Public benchmarks' },
  modelClasses: { academic: 'Academic research', 'open-weights': 'Open-weights models', commercial: 'Commercial use' },
  peoplePresent: {
    'only-me': 'Only me',
    'all-consented': 'Other people too, and every one of them agreed to this use',
    'no-identifiable-people': 'No identifiable face or voice (e.g. hands only, scenery)',
  },
  compensation: { none: 'None', 'credit-only': 'Credit only', monetary: 'Payment' },
  rightsBasis: {
    'own-recording': 'I recorded it',
    licensed: 'Licensed from the person who recorded it',
    'other-rights-holder': 'Other documented rights',
  },
  labels: { porn: 'Porn', sexual: 'Sexual', nudity: 'Nudity', 'graphic-media': 'Graphic media' },
}
