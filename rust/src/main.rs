use corpus_preprocessing_core::phase2::{
    clean_to_files, load_json, parse_directory, pipeline_with_fuzzy, read_records, sample_records,
    scan_directory, write_json, write_jsonl, ProcessingError, SamplePlanV1,
};
use corpus_preprocessing_core::fuzzy::fuzzy_clean_file;
use corpus_preprocessing_core::{profile_jsonl, validate_jsonl};
use serde::Serialize;
use serde_json::{to_value, Value};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::path::Path;
use std::process;

fn usage() -> ! {
    eprintln!("usage: corpus-preprocessing-core <validate|profile|scan|parse|sample|clean|fuzzy-clean|pipeline> OPTIONS");
    process::exit(2);
}

fn options(values: &[String]) -> BTreeMap<&str, &str> {
    if values.len() % 2 != 0 { usage(); }
    let mut options = BTreeMap::new();
    for pair in values.chunks_exact(2) {
        if !pair[0].starts_with("--") || options.insert(pair[0].as_str(), pair[1].as_str()).is_some() { usage(); }
    }
    options
}

fn required<'a>(options: &'a BTreeMap<&str, &str>, key: &str) -> &'a str {
    options.get(key).copied().unwrap_or_else(|| usage())
}

fn values<T: Serialize>(items: &[T]) -> Vec<Value> {
    items.iter().map(|item| to_value(item).expect("serialization must succeed")).collect()
}

fn run(args: &[String]) -> Result<(), ProcessingError> {
    if args.len() < 2 { usage(); }
    let options = options(&args[2..]);
    match args[1].as_str() {
        "validate" if options.len() == 1 => {
            let path = required(&options, "--input");
            let input = fs::read_to_string(path).map_err(|error| ProcessingError::new("io_error", error.to_string()))?;
            let records = validate_jsonl(&input).map_err(|error| ProcessingError::new(error.error.error_type, error.to_string()))?;
            println!("validated {} records", records.len());
        }
        "profile" if options.len() == 2 => {
            let input_path = required(&options, "--input");
            let output_path = required(&options, "--output");
            let input = fs::read_to_string(input_path).map_err(|error| ProcessingError::new("io_error", error.to_string()))?;
            let profile = profile_jsonl(&input).map_err(|error| ProcessingError::new(error.error.error_type, error.to_string()))?;
            write_json(Path::new(output_path), &to_value(&profile).expect("profile serializes"))?;
            println!("profiled {} records -> {}", profile.total_records, output_path);
        }
        "scan" if options.len() == 2 => {
            let input = Path::new(required(&options, "--input"));
            let output = Path::new(required(&options, "--output"));
            let manifest = scan_directory(input, Some(output))?;
            write_jsonl(output, &values(&manifest))?;
            println!("scanned {} files -> {}", manifest.len(), output.display());
        }
        "parse" if options.len() == 4 => {
            let input = Path::new(required(&options, "--input"));
            let config = load_json(Path::new(required(&options, "--config")))?;
            let records_output = Path::new(required(&options, "--records-output"));
            let quarantine_output = Path::new(required(&options, "--quarantine-output"));
            let (records, quarantine) = parse_directory(input, &config)?;
            write_jsonl(records_output, &values(&records))?;
            write_jsonl(quarantine_output, &values(&quarantine))?;
            println!("parsed {} records; quarantined {}", records.len(), quarantine.len());
        }
        "sample" if options.len() == 4 => {
            let records = read_records(Path::new(required(&options, "--input")))?;
            let plan = SamplePlanV1::from_value(load_json(Path::new(required(&options, "--plan")))?)?;
            let (selected, report) = sample_records(&records, &plan)?;
            write_jsonl(Path::new(required(&options, "--output")), &values(&selected))?;
            write_json(Path::new(required(&options, "--report")), &report)?;
            println!("sampled {} of {} records", selected.len(), report["filtered_population_size"]);
        }
        "clean" if options.len() == 5 => {
            let (cleaned, events, cache_hit) = clean_to_files(
                Path::new(required(&options, "--input")), Path::new(required(&options, "--config")),
                Path::new(required(&options, "--output")), Path::new(required(&options, "--events")),
                Path::new(required(&options, "--run-manifest")),
            )?;
            println!("cleaned {} records; events {}; cache_hit={}", cleaned.len(), events.len(), cache_hit);
        }
        "fuzzy-clean" if options.len() == 3 => {
            let report = fuzzy_clean_file(
                Path::new(required(&options, "--input")), Path::new(required(&options, "--config")),
                Path::new(required(&options, "--output-dir")),
            )?;
            println!("fuzzy-cleaned {} records; decisions={}", report["record_count"], report["counts"]);
        }
        "pipeline" if options.len() == 5 || options.len() == 6 => {
            let result = pipeline_with_fuzzy(
                Path::new(required(&options, "--input")), Path::new(required(&options, "--parsing-config")),
                Path::new(required(&options, "--cleaning-config")), Path::new(required(&options, "--sample-plan")),
                Path::new(required(&options, "--output-dir")),
                options.get("--fuzzy-config").map(|path| Path::new(*path)),
            )?;
            println!("pipeline completed; output_fingerprint={}", result["outputs"]["cleaned-records.jsonl"]);
        }
        _ => usage(),
    }
    Ok(())
}

fn main() {
    if let Err(error) = run(&env::args().collect::<Vec<_>>()) {
        eprintln!("{error}");
        process::exit(1);
    }
}
